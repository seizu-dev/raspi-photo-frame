"""
S3 互換ストレージ（GCS の XML API・MinIO・R2 等）を写真取得元として扱う PhotoProvider 実装

`S3_BUCKET` の `S3_PREFIX` 以下を写真のルートとし、ルート直下のサブフォルダ
（`delimiter=/` の CommonPrefixes）をアルバム、ルート直下に画像が直接あれば
「未分類」の仮想アルバムを先頭へ1件差し込む（Drive の `gdrive_api.py` /
ローカルの `local_api.py` と同じ構造・同じ ID 形式）。

**追加依存は無い。** 署名（SigV4）は `hmac` / `hashlib` だけで書き、通信は
`requests`。**GET のみ・path-style のみ**（`<endpoint>/<bucket>/<key>`。GCS・MinIO・R2
とも受け付ける。仮想ホスト形式は実装しない）。

**requests の `params=` を使わない。** requests は空白を `+` で送るが署名の正規化は
`%20` なので、空白を含むキー（例: `photos/2106 Pecorine/`）で署名エラーになる。
URL のパスとクエリは署名と同じ quote 規則で自分で組み立てる。

**写真本体は原本を取得して `PhotoCache`（原本モード）へ渡す。** EXIF 補正・画素上限・
JPEG の draft デコードは `PhotoCache` 側。HEIC/HEIF は原本をデコードすると 48MP で
ピーク約602MB（PoC 実測）になり実機の RAM 416MB で成り立たないため、拡張子で除外して
件数だけログに残す（ローカル取得元と同じ方針。S3 には縮小済みの手段が無い）。

**アクセスキー・署名・Authorization ヘッダ・バケット名をログに出さない。** ログに残すのは
キー（オブジェクト名）とステータスだけ。S3 のエラー本文は `<Message>` に署名の材料を
含みうるため、`<Code>` だけを拾う。
"""

import hashlib
import hmac
import logging
import os
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests

from src.photo_provider import ProviderError
from src.provider_util import b64d as _b64d
from src.provider_util import b64e as _b64e
from src.provider_util import display_name as _display
from src.provider_util import sha_prefix as _sha

logger = logging.getLogger(__name__)

DEFAULT_REGION = 'auto'
SERVICE = 's3'
EMPTY_SHA256 = hashlib.sha256(b'').hexdigest()

# immich_api.py / gdrive_api.py の DEFAULT_TIMEOUT と揃える
DEFAULT_TIMEOUT = 30
# 原本のストリーミング取得の上限（gdrive_api.MAX_ORIGINAL_BYTES と同じ値。あちらは
# google-auth を import するため、ここでは複製して依存を避ける）。超えたら中断する。
MAX_ORIGINAL_BYTES = 40 * 1024 * 1024
# ListObjectsV2 の1ページの件数（S3 の上限は1000）と、辿るページ数の上限
LIST_MAX_KEYS = 1000
MAX_LIST_PAGES = 200

IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
SKIPPED_EXTENSIONS = ('.heic', '.heif')

ROOT_ALBUM_ID = 'r'
COVER_ID = 'cover'

# PhotoCache._safe_name() は 120 文字で切るため、ID 全体を 120 文字以内に収める
MAX_ID_LEN = 120
MAX_EMBED_B64_LEN = 96


# ---------------------------------------------------------------- SigV4

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def canonical_query(query: dict[str, str]) -> str:
    """ 署名用（兼 URL 用）のクエリ文字列。空白は `%20`、予約文字は全てエンコードする """
    pairs = sorted((quote(k, safe='~'), quote(str(v), safe='~')) for k, v in query.items())
    return '&'.join(f'{k}={v}' for k, v in pairs)


def sign_request(method: str, path: str, query: dict[str, str], headers: dict[str, str],
                 access_key: str, secret_key: str, region: str, amz_date: str,
                 payload_hash: str = EMPTY_SHA256, service: str = SERVICE) -> str:
    """
    SigV4 の Authorization ヘッダ値を返す。`headers` は署名に含めるヘッダ
    （host / x-amz-date / x-amz-content-sha256 等）。`path` は未エンコードの絶対パス。
    AWS 公式のテストベクタと一致することを検証済み（S3 はパスを二重エンコードしない）。
    """
    date = amz_date[:8]
    lower = {k.lower().strip(): ' '.join(str(v).split()) for k, v in headers.items()}
    signed = ';'.join(sorted(lower))
    canon_headers = ''.join(f'{k}:{lower[k]}\n' for k in sorted(lower))
    creq = '\n'.join([method, quote(path, safe='/~'), canonical_query(query),
                      canon_headers, signed, payload_hash])
    scope = f'{date}/{region}/{service}/aws4_request'
    sts = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope,
                     hashlib.sha256(creq.encode('utf-8')).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(('AWS4' + secret_key).encode('utf-8'), date), region),
                    service), 'aws4_request')
    sig = hmac.new(k, sts.encode('utf-8'), hashlib.sha256).hexdigest()
    return (f'AWS4-HMAC-SHA256 Credential={access_key}/{scope}, '
            f'SignedHeaders={signed}, Signature={sig}')


def _xml_tag(tag: str) -> str:
    """ `{namespace}Name` から名前空間を外す（GCS / S3 の応答は名前空間付き） """
    return tag.rsplit('}', 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _xml_tag(child.tag) == name:
            return child.text or ''
    return ''


class S3API:
    """
    S3 互換ストレージを扱うクラス（`PhotoProvider` を満たす）。

    ID の設計は `local_api.LocalFolderAPI` と同じ（キャッシュのファイル名になるため）:

    - アルバム ID: ルート `'r'` / フォルダ `'d.<base64(フォルダ名)>'`、長すぎるときは
      `'h.<sha1先頭24文字>'`（対応表で引く。無ければルートを一覧して作る）
    - アセット ID: `'<sig8>.p<base64(ルートからの相対キー)>'` または
      `'<sig8>.h<sha1先頭24文字>'`。`sig8` は **ETag**（無ければサイズ）のハッシュで、
      オブジェクトを差し替えるとキャッシュキーが変わる
    """

    def __init__(self, settings_manager: Any = None,
                 status_callback: Callable[[str], None] | None = None) -> None:
        self.name = 's3'
        self.cache_namespace = 's3'
        self.supports_favorites = False
        self.delivers_originals = True
        # 表紙は名前順の先頭画像（'cover' 固定）。先頭が変わっても ID は変わらないため
        # Drive / ローカルと同じく期限で取り直す（photo_provider.py の Protocol 参照）
        self.album_thumbnail_expires = True
        # 一覧は通信が高価（Drive と同じ）。cache_lifetime_hours に従って取り直す
        self.rescan_on_load = False

        self.settings = settings_manager
        self._status_callback = status_callback

        endpoint = (os.environ.get('S3_ENDPOINT_URL') or '').strip().rstrip('/')
        bucket = (os.environ.get('S3_BUCKET') or '').strip()
        access_key = (os.environ.get('S3_ACCESS_KEY_ID') or '').strip()
        secret_key = (os.environ.get('S3_SECRET_ACCESS_KEY') or '').strip()
        missing = [n for n, v in (('S3_ENDPOINT_URL', endpoint), ('S3_BUCKET', bucket),
                                  ('S3_ACCESS_KEY_ID', access_key),
                                  ('S3_SECRET_ACCESS_KEY', secret_key)) if not v]
        if missing:
            raise ValueError(f'S3 の設定が足りません: {", ".join(missing)}')
        if urlsplit(endpoint).scheme not in ('http', 'https') or not urlsplit(endpoint).netloc:
            raise ValueError('S3_ENDPOINT_URL は http(s):// で始まる URL を指定してください')

        self._endpoint = endpoint
        self._host = urlsplit(endpoint).netloc
        self._base_path = urlsplit(endpoint).path.rstrip('/')
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = (os.environ.get('S3_REGION') or '').strip() or DEFAULT_REGION
        prefix = (os.environ.get('S3_PREFIX') or '').strip().lstrip('/')
        self._root = prefix if not prefix or prefix.endswith('/') else prefix + '/'
        self._max_keys = LIST_MAX_KEYS

        self._table_lock = threading.Lock()
        # ハッシュ形式の ID -> ルートからの相対名（アルバムはフォルダ名、アセットは相対キー）
        self._hash_albums: dict[str, str] = {}
        self._hash_assets: dict[str, str] = {}
        # 対応表で引けなかったハッシュ ID の digest。同じ digest のたびに一覧を取り直すと
        # 通信が高価なため記憶する（新しく一覧を取得して表を作り直したらクリア）
        self._missing_albums: set[str] = set()
        self._missing_assets: set[str] = set()
        # 上限超過で弾いたキー（Drive の _skipped_originals と同じ形。再取得を避ける）
        self._oversized: set[str] = set()
        self._oversized_lock = threading.Lock()

    def update_status(self, message: str) -> None:
        """ 進捗を通知する。コールバック未指定でも実機の調査手段としてログには必ず残す """
        logger.info(message)
        if self._status_callback:
            self._status_callback(message)

    # -------------------------------------------------------------------- HTTP

    def _get(self, key: str, query: dict[str, str] | None = None,
             stream: bool = False) -> requests.Response:
        """
        署名付き GET。`key` はバケット内のキー（空なら一覧）。通信断は `ProviderError`
        （型名だけ。URL にはバケット名が入るためメッセージに載せない）。
        """
        query = query or {}
        path = f'{self._base_path}/{self._bucket}' + (f'/{key}' if key else '/')
        amz_date = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        headers = {'host': self._host, 'x-amz-content-sha256': EMPTY_SHA256,
                   'x-amz-date': amz_date}
        auth = sign_request('GET', path, query, headers, self._access_key,
                            self._secret_key, self._region, amz_date)
        url = f"{urlsplit(self._endpoint).scheme}://{self._host}{quote(path, safe='/~')}"
        if query:
            url += '?' + canonical_query(query)
        send = {'x-amz-content-sha256': EMPTY_SHA256, 'x-amz-date': amz_date,
                'Authorization': auth}
        try:
            return requests.get(url, headers=send, timeout=DEFAULT_TIMEOUT, stream=stream)
        except requests.exceptions.RequestException as e:
            # from None: 例外連鎖の URL（バケット名を含む）が logger.exception 等で漏れないように断つ
            raise ProviderError(f'S3 への接続に失敗しました: {type(e).__name__}') from None

    @staticmethod
    def _error_code(response: requests.Response) -> str:
        """ エラー応答の `<Code>` だけを返す（`<Message>` は署名の材料を含みうるため読まない） """
        try:
            root = ET.fromstring(response.content[:65536])
        except ET.ParseError:
            return ''
        return _child_text(root, 'Code')[:64]

    def _check(self, response: requests.Response, what: str) -> None:
        if response.status_code < 400:
            return
        code = self._error_code(response)
        response.close()
        raise ProviderError(f'S3 の{what}に失敗しました: HTTP {response.status_code}'
                            + (f' {code}' if code else ''))

    def _list(self, prefix: str) -> tuple[list[str], list[dict[str, str]]]:
        """
        `prefix` 直下（`delimiter=/`）を全ページ辿り、(サブフォルダの prefix, オブジェクト) を
        返す。オブジェクトは `{'key', 'etag', 'size'}`。キーが `/` で終わるもの
        （コンソールで作ったフォルダ用の空オブジェクト）は含めない。
        """
        prefixes: list[str] = []
        objects: list[dict[str, str]] = []
        token = ''
        for _ in range(MAX_LIST_PAGES):
            query = {'list-type': '2', 'delimiter': '/', 'prefix': prefix,
                     'max-keys': str(self._max_keys)}
            if token:
                query['continuation-token'] = token
            response = self._get('', query)
            self._check(response, '一覧取得')
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as e:
                raise ProviderError('S3 の一覧応答を解析できませんでした') from None
            finally:
                response.close()
            truncated = False
            token = ''
            for el in root:
                tag = _xml_tag(el.tag)
                if tag == 'Contents':
                    key = _child_text(el, 'Key')
                    if key and not key.endswith('/'):
                        objects.append({'key': key,
                                        'etag': _child_text(el, 'ETag').strip('"'),
                                        'size': _child_text(el, 'Size')})
                elif tag == 'CommonPrefixes':
                    p = _child_text(el, 'Prefix')
                    if p:
                        prefixes.append(p)
                elif tag == 'IsTruncated':
                    truncated = (el.text or '').strip().lower() == 'true'
                elif tag == 'NextContinuationToken':
                    token = el.text or ''
            if not truncated or not token:
                return prefixes, objects
        logger.warning('S3 の一覧が上限 %d ページを超えたため打ち切りました: prefix=%s',
                       MAX_LIST_PAGES, _display(prefix))
        return prefixes, objects

    # ------------------------------------------------------------ ID・分類の補助

    @staticmethod
    def _ext(key: str) -> str:
        return os.path.splitext(key)[1].lower()

    def _split_images(self, objects: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
        """ 対応画像だけを名前順で返す（隠しファイルは除く）。HEIC の件数も返す """
        images: list[dict[str, str]] = []
        skipped = 0
        for obj in objects:
            name = obj['key'].rsplit('/', 1)[-1]
            if name.startswith('.'):
                continue
            ext = self._ext(name)
            if ext in SKIPPED_EXTENSIONS:
                skipped += 1
            elif ext in IMAGE_EXTENSIONS:
                images.append(obj)
        images.sort(key=lambda o: o['key'])
        return images, skipped

    def _album_name(self, prefix: str) -> str:
        return prefix[len(self._root):].rstrip('/')

    def _album_id_for(self, name: str) -> str:
        token = _b64e(name)
        if len(token) <= MAX_EMBED_B64_LEN:
            return f'd.{token}'
        digest = _sha(name, 24)
        with self._table_lock:
            self._hash_albums[digest] = name
        return f'h.{digest}'

    def _resolve_album_prefix(self, album_id: str, notify: bool = True) -> str | None:
        """ アルバム ID からバケット内の prefix を引く。不正な ID・存在しないものは None """
        if album_id == ROOT_ALBUM_ID:
            return self._root
        if album_id.startswith('d.'):
            try:
                name = _b64d(album_id[2:])
            except (ValueError, UnicodeDecodeError):
                return None
        elif album_id.startswith('h.'):
            digest = album_id[2:]
            with self._table_lock:
                name = self._hash_albums.get(digest)
                known_missing = digest in self._missing_albums
            if name is None and known_missing:
                return None
            if name is None:
                # 再起動後にキャッシュ済みの写真リストの ID を引く経路。ルートを
                # 一覧し直して対応表を作る（_fetch_albums が表へ登録する）
                self._fetch_albums(notify)
                with self._table_lock:
                    name = self._hash_albums.get(digest)
                    if name is None:
                        self._missing_albums.add(digest)
            if name is None:
                return None
        else:
            return None
        # フォルダ名は1階層のみ。'/' を含む細工 ID はここで弾く
        if not name or '/' in name:
            return None
        return f'{self._root}{name}/'

    def _asset_id_for(self, obj: dict[str, str]) -> str:
        rel = obj['key'][len(self._root):]
        sig = _sha(obj['etag'] or f"size:{obj['size']}", 8)
        token = _b64e(rel)
        if len(token) <= MAX_EMBED_B64_LEN:
            asset_id = f'{sig}.p{token}'
        else:
            digest = _sha(rel, 24)
            asset_id = f'{sig}.h{digest}'
            with self._table_lock:
                self._hash_assets[digest] = rel
        assert len(asset_id) <= MAX_ID_LEN
        return asset_id

    def _resolve_asset_key(self, asset_id: str) -> str | None:
        """ アセット ID からバケット内のキーを引く。引けなければ None """
        _, dot, tail = asset_id.partition('.')
        if not dot or not tail:
            return None
        if tail[0] == 'p':
            try:
                rel = _b64d(tail[1:])
            except (ValueError, UnicodeDecodeError):
                return None
        elif tail[0] == 'h':
            digest = tail[1:]
            with self._table_lock:
                rel = self._hash_assets.get(digest)
                known_missing = digest in self._missing_assets
            if rel is None and known_missing:
                return None
            if rel is None:
                rebuilt = self._rebuild_hash_assets()
                with self._table_lock:
                    rel = self._hash_assets.get(digest)
                    # 一覧に失敗したときは記憶しない（次回は取り直せるように）
                    if rel is None and rebuilt:
                        self._missing_assets.add(digest)
            if rel is None:
                return None
        else:
            return None
        if not rel or rel.endswith('/') or rel.count('/') > 1:
            return None
        if self._ext(rel) not in IMAGE_EXTENSIONS:
            return None
        return f'{self._root}{rel}'

    def _rebuild_hash_assets(self) -> bool:
        """ 対応表に無いハッシュ ID を引くため、深さ2の木を一覧して作り直す。成功なら True """
        try:
            prefixes, objects = self._list(self._root)
            for p in prefixes:
                objects = objects + self._list(p)[1]
        except ProviderError as e:
            logger.warning('S3 の一覧取得に失敗しました: %s', e)
            return False
        with self._table_lock:
            self._missing_assets.clear()
        for obj in objects:
            self._asset_id_for(obj)
        return True

    # ------------------------------------------------------ PhotoProvider Protocol

    def fetch_albums(self) -> list[dict[str, Any]]:
        """
        ルート直下のサブフォルダをアルバムとして返す（名前順）。ルート直下に対応画像が
        あれば先頭に「未分類」仮想アルバムを差し込む。**一覧を取れないときは
        `ProviderError`**（ローカル取得元と同じ契約。`album.py` のワーカーが握る）。
        """
        return self._fetch_albums(True)

    def _fetch_albums(self, notify: bool) -> list[dict[str, Any]]:
        """ fetch_albums の本体。`notify` が False なら失敗時にステータスのトーストを出さない """
        try:
            prefixes, objects = self._list(self._root)
        except ProviderError as e:
            if notify:
                self.update_status('アルバム取得エラー')
            logger.warning('%s', e)
            raise
        with self._table_lock:
            self._missing_albums.clear()
        albums = [{'id': self._album_id_for(self._album_name(p)),
                   'albumName': _display(self._album_name(p)),
                   'albumThumbnailAssetId': COVER_ID}
                  for p in sorted(prefixes)]
        images, _ = self._split_images(objects)
        if images:
            # albumName は空文字のまま保つ（album.py の name_key 契約）
            albums.insert(0, {'id': ROOT_ALBUM_ID, 'albumName': '',
                              'name_key': 'album.s3_root',
                              'albumThumbnailAssetId': COVER_ID})
        return albums

    def fetch_album_assets(self, album_id: str, album_name: str = '',
                           apply_album_order: bool = True) -> list[dict[str, Any]]:
        """
        フォルダ直下（再帰しない）の画像をキー名順で返す。`apply_album_order` は
        並び順設定が無いため無視する（Protocol 互換のためだけに残す）。
        撮影日（`date`）は一覧では分からない（原本を開かないと読めない）ため空文字で、
        表示時に `PhotoCache` が残した値で補う。フォルダが無ければ空リスト、
        通信エラーは `ProviderError`。
        """
        prefix = self._resolve_album_prefix(album_id)
        if prefix is None:
            logger.info('アルバムが見つかりません（空として扱います）: %s', album_id[:40])
            return []
        _, objects = self._list(prefix)
        images, skipped = self._split_images(objects)
        album_name = _display(album_name)
        if skipped:
            logger.info('HEIC/HEIF は原本をデコードしないためスキップしました: %d 件 (%s)',
                        skipped, album_name or album_id[:40])
        assets: list[dict[str, Any]] = []
        for obj in images:
            info: dict[str, Any] = {'id': self._asset_id_for(obj), 'description': '',
                                    'date': ''}
            if album_name:
                info['album_name'] = album_name
            assets.append(info)
        return assets

    def fetch_favorite_assets(self) -> list[dict[str, Any]]:
        """ S3 はお気に入りに対応しない。呼ばれたら空リスト """
        logger.warning('S3 はお気に入りに対応していません。呼び出しを無視します')
        return []

    def _fetch_original(self, key: str) -> bytes | None:
        """
        原本を上限付きでストリーミング取得する。404（消えた）・上限超過は None
        （1枚落ちても続ける）。認証・5xx・通信エラーは `ProviderError`。
        """
        with self._oversized_lock:
            if key in self._oversized:
                return None
        response = self._get(key, stream=True)
        try:
            if response.status_code == 404:
                logger.warning('オブジェクトが見つかりません: %s', _display(key))
                return None
            self._check(response, '原本取得')
            length = response.headers.get('Content-Length', '')
            if length.isdigit() and int(length) > MAX_ORIGINAL_BYTES:
                logger.warning('原本が上限 %dMB を超えているためスキップします: %s',
                               MAX_ORIGINAL_BYTES // (1024 * 1024), _display(key))
                with self._oversized_lock:
                    self._oversized.add(key)
                return None
            buf = BytesIO()
            try:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    buf.write(chunk)
                    if buf.tell() > MAX_ORIGINAL_BYTES:
                        logger.warning('原本のダウンロードが上限 %dMB を超えたため中断しました: %s',
                                       MAX_ORIGINAL_BYTES // (1024 * 1024), _display(key))
                        with self._oversized_lock:
                            self._oversized.add(key)
                        return None
            except requests.exceptions.RequestException as e:
                raise ProviderError(
                    f'原本のダウンロード中にエラーが発生しました: {type(e).__name__}') from None
            return buf.getvalue()
        finally:
            response.close()

    def fetch_photo(self, asset_id: str) -> bytes | None:
        """ 原本を取得して返す（縮小・補正は `PhotoCache` が行う）。引けなければ None """
        key = self._resolve_asset_key(asset_id)
        if key is None:
            logger.warning('アセット ID を解決できません: %s', asset_id[:40])
            return None
        return self._fetch_original(key)

    def fetch_album_thumbnail(self, album: dict[str, Any]) -> bytes | None:
        """ フォルダをキー名順にした先頭の画像（JPEG を優先）の原本を返す """
        album_id = album.get('id')
        if not album_id:
            return None
        try:
            prefix = self._resolve_album_prefix(album_id, notify=False)
        except ProviderError:
            # 例外の内容はバケット名を含みうるため出さない（_fetch_albums が警告済み）
            logger.warning('アルバムサムネイル用のフォルダ解決に失敗しました')
            return None
        if prefix is None:
            return None
        try:
            _, objects = self._list(prefix)
        except ProviderError as e:
            logger.warning('アルバムサムネイル用の一覧取得に失敗しました: %s', e)
            return None
        images, _ = self._split_images(objects)
        if not images:
            return None
        images.sort(key=lambda o: (0 if self._ext(o['key']) in ('.jpg', '.jpeg') else 1,
                                   o['key']))
        return self._fetch_original(images[0]['key'])
