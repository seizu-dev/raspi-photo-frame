"""
Google Drive の共有フォルダを写真取得元として扱う PhotoProvider 実装

`.claude/plans/abundant-weaving-kernighan.md` PR2。PoC（`tools/verification/
gdrive_probe.py`、ブランチ feat/gdrive-poc）で確定した方針を実装する。

**サーバー側の縮小（`thumbnailLink`）を優先し、原本は例外経路として扱う。**
Drive の `thumbnailLink` に `=wW-hH` を付けると、Drive 側で EXIF/HEIF の回転を
適用したうえで、アスペクト比を保って W×H に収まる寸法へ縮小した画像を返す
（HEIC も JPEG/PNG へ変換される）。転送量は原本の約 1/30 で済み、HEIC を
このプロセスでデコードする必要が無くなる（48MP の HEIC を原本からデコードすると
ピーク約602MB、20MP でも約211MB になることを PoC で実測しており、実機の
RAM 416MB では成り立たない）。`thumbnailLink` が使えない場合だけ、
JPEG/PNG/WebP に限って原本へフォールバックする（HEIC はスキップする）。

**google-auth は遅延 import する。** Immich のみを使う環境（実機の既定構成）の
常駐メモリを増やさないため、`import` はこのモジュールのトップレベルではなく
実際にトークンを扱う関数の中でだけ行う。`google_auth` の import コストは
`photo_provider.create_provider()` が `PF_PHOTO_PROVIDER=gdrive` のときだけ
このモジュール自体を遅延 import することと合わせて、二重に「使わなければ
読み込まれない」形にしてある。

写真の ID は `<fileId>.<md5の先頭8文字>`（md5 が無い場合は modifiedTime のハッシュで
代用）。`thumbnailLink` は数時間で失効する署名付き URL のため、写真リストの
JSON には保存せず、表示のたびに `files.get` で取り直す。
"""

import hashlib
import logging
import math
import os
import threading
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

import requests
from PIL import Image, UnidentifiedImageError

from src.photo_cache import (
    DEFAULT_DISPLAY_SIZE,
    FIT_CONTAIN,
    FIT_COVER,
    normalize_fit,
    resolve_fit,
)
from src.photo_provider import ProviderError

if TYPE_CHECKING:
    from src.config_manager import ConfigManager

logger = logging.getLogger(__name__)

# サービスアカウント鍵の既定ファイル名。config ディレクトリ直下に置く
# （.gitignore の /config/*.json で除外し、settings.sample.json だけ例外にしてある）。
DEFAULT_SA_KEY_FILENAME = 'gdrive-service-account.json'

DRIVE_API_BASE = 'https://www.googleapis.com/drive/v3'
DRIVE_SCOPES = ('https://www.googleapis.com/auth/drive.readonly',)
FOLDER_MIME = 'application/vnd.google-apps.folder'

# 対応する画像 MIME（一覧・アセット化の対象）。HEIC/HEIF は thumbnailLink 経由でのみ
# 表示できる（原本はデコードしない）。
ALLOWED_IMAGE_MIMES = ('image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif')
# 原本経路で扱ってよい形式。HEIC/HEIF はここに含めない
# （画素上限なしで原本をデコードしない、という禁止パターンに従う）。
ORIGINAL_MIME_ALLOWED = ('image/jpeg', 'image/png', 'image/webp')

# immich_api.py の DEFAULT_TIMEOUT と揃える
DEFAULT_TIMEOUT = 30
# トークンの有効期限が切れる何秒前から強制更新するか
TOKEN_REFRESH_MARGIN_SEC = 300
# 原本のストリーミング取得の上限。超えたら中断する（RAM 512MB が最大の制約のため、
# 上限の無いダウンロードを許さない）。
MAX_ORIGINAL_BYTES = 40 * 1024 * 1024

LIST_ALBUM_FIELDS = 'nextPageToken,files(id,name,mimeType)'
LIST_ASSET_FIELDS = ('nextPageToken,files(id,name,mimeType,md5Checksum,modifiedTime,'
                      'imageMediaMetadata(time))')
LIST_THUMB_FIELDS = 'nextPageToken,files(id,name,mimeType,thumbnailLink)'
GET_PHOTO_META_FIELDS = 'mimeType,thumbnailLink,imageMediaMetadata(width,height,rotation)'


def _redact(url: str) -> str:
    """ 署名付き URL をログに出さないため、ホスト名だけを残す """
    return urlsplit(url).netloc


class GDriveAPI:
    """
    Google Drive の共有フォルダとの通信を管理するクラス（`PhotoProvider` を満たす）。

    ルート直下のサブフォルダをアルバムとして扱い、ルート直下に写真が直接あれば
    「未分類」の仮想アルバムとして1件差し込む（`album.gdrive_root` の翻訳キー。
    `src/gui/screens/album.py` の `name_key` 契約に乗る）。
    """

    def __init__(self, settings_manager: 'ConfigManager',
                 status_callback: Callable[[str], None] | None = None,
                 key_file: str | None = None, root_folder_id: str | None = None) -> None:
        self.name = 'gdrive'
        self.cache_namespace = 'gdrive'
        self.supports_favorites = False
        self.delivers_originals = True
        # フォルダの表紙は常に「名前順の先頭画像」（albumThumbnailAssetId='cover' 固定）を
        # 指す。Immich と違い、先頭画像が入れ替わっても albumThumbnailAssetId 自体は
        # 変化しないため、album.py 側で有効期限を見て定期的に作り直す必要がある
        # （photo_provider.py の Protocol docstring 参照）。
        self.album_thumbnail_expires = True

        self.settings = settings_manager
        self._status_callback = status_callback
        self._display_size: tuple[int, int] = DEFAULT_DISPLAY_SIZE

        key_path = key_file or os.environ.get('GDRIVE_SA_KEY_FILE')
        if not key_path:
            # config_manager.py と同じ規則（PF_CONFIG_DIR 未設定なら ./config）で
            # 既定の鍵ファイルパスを決める。秘匿情報を bind mount 経由で渡す方針
            # （.claude/architecture.md「秘匿情報をイメージに焼き込まない」）に合わせる。
            from src.config_manager import resolve_config_dir
            key_path = str(resolve_config_dir() / DEFAULT_SA_KEY_FILENAME)
        self.key_file = key_path

        self.root_folder_id = (root_folder_id or os.environ.get('GDRIVE_ROOT_FOLDER_ID') or '').strip()
        if not self.root_folder_id:
            raise ValueError('GDRIVE_ROOT_FOLDER_ID を .env に設定してください。')

        if not Path(self.key_file).is_file():
            raise ValueError(
                f'Google Drive のサービスアカウント鍵ファイルが見つかりません: {self.key_file}'
                '（GDRIVE_SA_KEY_FILE で場所を指定するか、既定の場所に置いてください）')

        # google-auth はここで初めて import する（モジュール冒頭のコメント参照）。
        try:
            from google.oauth2.service_account import Credentials
        except ImportError as e:
            raise ValueError(
                f'google-auth を import できません（requirements.txt を確認してください）: {e}') from e

        try:
            self._credentials = Credentials.from_service_account_file(
                self.key_file, scopes=list(DRIVE_SCOPES))
        except (ValueError, OSError) as e:
            # 中身が壊れた JSON・鍵の形式不備など。鍵の中身自体はログに出さない
            raise ValueError(f'サービスアカウント鍵ファイルを読み込めません: {self.key_file} ({e})') from e

        self._token_lock = threading.Lock()
        # 一度失敗し「原本もデコードできない」と分かった fileId。次回は
        # メタデータ取得すら行わず即座にスキップする（同じ失敗を繰り返さないため）。
        self._skipped_originals: set[str] = set()

    def update_status(self, message: str) -> None:
        """ 進捗を通知する。コールバック未指定でも実機の調査手段としてログには必ず残す """
        logger.info(message)
        if self._status_callback:
            self._status_callback(message)

    def set_display_size(self, size: tuple[int, int]) -> None:
        """
        表示解像度を伝える。`PhotoCache` は provider より後に生成されるため、
        main.py / warm_cache.py がキャッシュ生成後にこれを呼ぶ。未呼び出しの場合は
        `DEFAULT_DISPLAY_SIZE`（1024x600）のまま動作する。
        """
        self._display_size = size

    # ------------------------------------------------------------------ 認証

    def _token_expires_soon(self) -> bool:
        expiry = self._credentials.expiry
        if expiry is None:
            return False
        return (expiry - datetime.utcnow()).total_seconds() < TOKEN_REFRESH_MARGIN_SEC

    def _refresh_locked(self) -> None:
        """ 呼び出し側が `self._token_lock` を保持している前提でトークンを更新する """
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
        try:
            self._credentials.refresh(Request())
        except GoogleAuthError as e:
            # 例外本文は含めない（.claude/coding-style.md「秘匿情報をログに残さない」と同じ
            # 理由で、ProviderError のメッセージは photo_source.py 経由で画面のステータス
            # トーストにそのまま出る経路があるため。他の ProviderError と揃えて型名だけにする）。
            raise ProviderError(f'Google Drive のトークン更新に失敗しました: {type(e).__name__}') from e

    def _get_token(self, force: bool = False) -> str:
        """ スレッドセーフにアクセストークンを返す。期限の数分前に自動更新する """
        with self._token_lock:
            if force or not self._credentials.valid or self._token_expires_soon():
                self._refresh_locked()
            return self._credentials.token

    # -------------------------------------------------------------------- HTTP

    def _authed_get(self, url: str, *, params: dict[str, Any] | None = None,
                    stream: bool = False) -> requests.Response:
        """
        認可ヘッダ付きで GET する。401 のときは1回だけトークンを強制更新して再試行する
        （プロアクティブな期限管理だけでは、サーバー側の時計との誤差などで
        401 が返ることがありうるための保険）。
        """
        last_response: requests.Response | None = None
        for attempt in range(2):
            token = self._get_token(force=attempt == 1)
            try:
                last_response = requests.get(
                    url, headers={'Authorization': f'Bearer {token}'},
                    params=params, timeout=DEFAULT_TIMEOUT, stream=stream)
            except requests.exceptions.RequestException as e:
                raise ProviderError(f'{type(e).__name__}: {_redact(url)}') from e
            if last_response.status_code == 401 and attempt == 0:
                continue
            return last_response
        assert last_response is not None  # ループは必ず1回は response を得る
        return last_response

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """ Drive REST API を1回呼び、JSON を返す。エラーは `ProviderError` に変換する """
        url = f'{DRIVE_API_BASE}/{path}'
        response = self._authed_get(url, params=params)
        if response.status_code in (403, 404):
            logger.warning(
                'Drive API が %d を返しました（フォルダが SA に共有されていない可能性があります）: path=%s',
                response.status_code, path)
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise ProviderError(f'{type(e).__name__}: HTTP {response.status_code} ({path})') from e
        try:
            return response.json()
        except ValueError as e:
            # requests.exceptions.JSONDecodeError は ValueError のサブクラス（json 標準の
            # JSONDecodeError も同様）なのでこれで両方拾える。本文（レスポンスの生データ）は
            # 含めない（画面のステータスにそのまま出る経路があるため。他の ProviderError と揃える）。
            raise ProviderError(f'{type(e).__name__}: {path}') from e

    def _list_folder(self, folder_id: str, *, mime_filter: str | None = None,
                     fields: str = LIST_ASSET_FIELDS, order_by: str | None = None,
                     page_size: int = 1000) -> list[dict[str, Any]]:
        """ folder_id 直下（サブフォルダを辿らない）を全件列挙する """
        query = f"'{folder_id}' in parents and trashed=false"
        if mime_filter:
            query += f' and ({mime_filter})'

        files: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                'q': query,
                'pageSize': page_size,
                'supportsAllDrives': 'true',
                'includeItemsFromAllDrives': 'true',
                'fields': fields,
            }
            if order_by:
                params['orderBy'] = order_by
            if page_token:
                params['pageToken'] = page_token
            data = self._request('files', params)
            files.extend(data.get('files', []))
            page_token = data.get('nextPageToken')
            if not page_token:
                break
        return files

    def _fetch_original(self, file_id: str) -> bytes | None:
        """
        原本を上限付きでストリーミング取得する。JPEG/PNG/WebP のみ呼ばれる契約
        （HEIC/HEIF は呼び出し側でスキップ済み）。
        """
        url = f'{DRIVE_API_BASE}/files/{file_id}'
        response = self._authed_get(url, params={'alt': 'media', 'supportsAllDrives': 'true'},
                                    stream=True)
        if response.status_code in (403, 404):
            logger.warning('原本取得で %d が返りました（フォルダが SA に共有されていない可能性があります）: id=%s',
                          response.status_code, file_id)
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise ProviderError(f'原本の取得に失敗しました (id={file_id}): HTTP {response.status_code}') from e

        buf = BytesIO()
        try:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                buf.write(chunk)
                if buf.tell() > MAX_ORIGINAL_BYTES:
                    logger.warning('原本のダウンロードが上限 %dMB を超えたため中断しました: id=%s',
                                  MAX_ORIGINAL_BYTES // (1024 * 1024), file_id)
                    return None
        except requests.exceptions.RequestException as e:
            raise ProviderError(f'原本のダウンロード中にエラーが発生しました (id={file_id}): '
                               f'{type(e).__name__}') from e
        finally:
            response.close()
        return buf.getvalue()

    # ------------------------------------------------------------- 補助（純粋関数寄り）

    def _current_fit(self) -> str:
        return normalize_fit(self.settings.get('photo_fit', FIT_CONTAIN))

    def _required_thumbnail_size(self, image_meta: dict[str, Any]) -> tuple[int | None, int | None]:
        """
        `thumbnailLink` に付ける `=wW-hH` の W/H を決める。

        `imageMediaMetadata.width/height` は回転前の値、`rotation` は 90° 単位の
        回数（PoC で確認済み）のため、まず見た目の向きへ入れ替えてから
        `resolve_fit()`（`photo_cache.py` の判定をそのまま再利用する）に渡す。

        contain は画面の寸法をそのまま要求すればよい（Drive 側が回転を適用した
        うえでアスペクト比を保って収める）。cover（smart で cover と判定された
        場合を含む）は、画面を覆うのに必要な寸法まで拡大して要求する
        （その箱に contain で収まる＝片辺がちょうど画面、もう片辺がはみ出す）。
        """
        width, height = image_meta.get('width'), image_meta.get('height')
        if not width or not height:
            return None, None
        rotation = image_meta.get('rotation') or 0
        if rotation % 2 == 1:
            width, height = height, width

        disp_w, disp_h = self._display_size
        fit = resolve_fit(self._current_fit(), (width, height), (disp_w, disp_h))
        if fit != FIT_COVER:
            return disp_w, disp_h

        scale = max(disp_w / width, disp_h / height)
        return max(1, math.ceil(width * scale)), max(1, math.ceil(height * scale))

    def _looks_large_enough(self, data: bytes, req_w: int | None, req_h: int | None) -> bool:
        """
        取得した画像が要求寸法に対して小さすぎないかを確認する。

        片辺だけ小さい（例: 幅は足りるが高さが足りない）場合は許容する。
        **両辺とも要求の90%未満のときだけ**「小さすぎる」とみなして原本経路へ
        切り替える（`resize` 系のわずかな丸め誤差を過剰に弾かないため）。
        """
        if req_w is None or req_h is None:
            return True
        try:
            with Image.open(BytesIO(data)) as img:
                w, h = img.size
        except (UnidentifiedImageError, OSError) as e:
            logger.warning('thumbnailLink の画像を開けませんでした: %s', e)
            return False
        if w < req_w * 0.9 and h < req_h * 0.9:
            logger.info('thumbnailLink の寸法が要求より小さすぎるため原本へ切り替えます: '
                       'got=%dx%d requested=%dx%d', w, h, req_w, req_h)
            return False
        return True

    @staticmethod
    def _normalize_time(value: Any) -> str:
        """ `imageMediaMetadata.time`（`YYYY:MM:DD HH:MM:SS`）を ISO へ正規化する。無ければ空文字 """
        if not value:
            return ''
        try:
            return datetime.strptime(value, '%Y:%m:%d %H:%M:%S').strftime('%Y-%m-%dT%H:%M:%S')
        except (ValueError, TypeError):
            logger.debug('imageMediaMetadata.time を解釈できません: %r', value)
            return ''

    @staticmethod
    def _file_id_from_asset_id(asset_id: str) -> str:
        """ `<fileId>.<md5先頭8文字>` から fileId を取り出す（fileId 自体は '.' を含まない） """
        return asset_id.rsplit('.', 1)[0] if '.' in asset_id else asset_id

    # ------------------------------------------------------ PhotoProvider Protocol

    def fetch_albums(self) -> list[dict[str, Any]]:
        """
        ルート直下のサブフォルダをアルバムとして返す（名前順）。ルート直下に
        画像が直接あれば、先頭に「未分類」の仮想アルバムを1件差し込む。

        Immich の `fetch_albums()` と同じく通信エラーは内部で握って空リストを
        返す（`fetch_album_assets`/`fetch_favorite_assets` と違い、Protocol の
        `fetch_albums()` は呼び出し側の多くが例外を想定していないため）。
        """
        try:
            entries = self._list_folder(self.root_folder_id, fields=LIST_ALBUM_FIELDS,
                                        order_by='name')
        except ProviderError as e:
            self.update_status(f'アルバム取得エラー: {e}')
            return []

        subfolders = sorted(
            (e for e in entries if e.get('mimeType') == FOLDER_MIME),
            key=lambda e: e.get('name') or '')
        albums: list[dict[str, Any]] = []
        for f in subfolders:
            file_id = f.get('id')
            if not file_id:
                # Drive API のレスポンスに id を持たない要素が来ることは本来無いはずだが、
                # 直接 [] で参照すると KeyError でアルバム一覧全体が壊れてしまう。
                # 1件読み飛ばすだけに留める（.get() に寄せる方針）。
                logger.warning('Drive のフォルダ一覧に id の無い要素があるため読み飛ばします: name=%r',
                               f.get('name'))
                continue
            albums.append({'id': file_id, 'albumName': f.get('name') or file_id,
                           'albumThumbnailAssetId': 'cover'})

        has_root_images = any(
            e.get('mimeType') != FOLDER_MIME and e.get('mimeType') in ALLOWED_IMAGE_MIMES
            for e in entries)
        if has_root_images:
            # albumName は空文字のまま保つ（album.py の name_key 契約。_confirm_selection()
            # は entry.get('albumName') or '' を保存するため、ここが空文字であることが
            # 「翻訳済み文字列を設定ファイルへ書き込まない」の前提になる）。
            albums.insert(0, {
                'id': self.root_folder_id,
                'albumName': '',
                'name_key': 'album.gdrive_root',
                'albumThumbnailAssetId': 'cover',
            })
        return albums

    def fetch_album_assets(self, album_id: str, album_name: str = '',
                           apply_album_order: bool = True) -> list[dict[str, Any]]:
        """
        フォルダ直下の画像を名前順で返す。

        `apply_album_order` は Immich の「アルバムの並び順設定（asc/desc）」に
        相当する概念が Drive のフォルダには無いため無視する（Protocol との
        互換のために引数だけ残す）。
        """
        mime_filter = ' or '.join(f"mimeType='{m}'" for m in ALLOWED_IMAGE_MIMES)
        entries = self._list_folder(album_id, mime_filter=mime_filter,
                                    fields=LIST_ASSET_FIELDS, order_by='name')
        entries.sort(key=lambda e: e.get('name') or '')

        assets: list[dict[str, Any]] = []
        for f in entries:
            file_id = f.get('id')
            if not file_id:
                # fetch_albums() と同じ理由で .get() に寄せる。id が無ければこの1件だけ
                # 読み飛ばし、残りのアセットは表示を続ける。
                logger.warning('Drive のファイル一覧に id の無い要素があるため読み飛ばします: name=%r',
                               f.get('name'))
                continue
            # md5 が無い場合は modifiedTime（無ければ fileId 自体）からハッシュを作る。
            # 写真リスト JSON には幅・高さ・MIME・thumbnailLink を保存しない
            # （常駐量を抑えるためと、thumbnailLink はどのみち数時間で失効するため）。
            digest = f.get('md5Checksum') or hashlib.sha1(
                (f.get('modifiedTime') or file_id).encode('utf-8')).hexdigest()
            info: dict[str, Any] = {
                'id': f'{file_id}.{digest[:8]}',
                'description': '',
                'date': self._normalize_time((f.get('imageMediaMetadata') or {}).get('time')),
            }
            if album_name:
                info['album_name'] = album_name
            assets.append(info)
        return assets

    def fetch_favorite_assets(self) -> list[dict[str, Any]]:
        """ Drive はお気に入りに対応していない（`supports_favorites = False`）。呼ばれたら空リスト """
        logger.warning('Google Drive はお気に入りに対応していません。呼び出しを無視します')
        return []

    def fetch_photo(self, asset_id: str) -> bytes | None:
        """
        写真本体を取得する。`thumbnailLink` を優先し、失敗したら原本へ切り替える
        （モジュール冒頭のコメント参照）。

        原本へ切り替える条件: リンクが無い / 4xx・5xx / 画像として開けない /
        返ってきた寸法が要求より小さすぎる。原本経路は JPEG/PNG/WebP のみで、
        HEIC/HEIF は対象外（一度失敗したら `_skipped_originals` に記録し、
        以降は `files.get` すら呼ばずに即座に None を返す）。
        """
        file_id = self._file_id_from_asset_id(asset_id)
        if file_id in self._skipped_originals:
            return None

        meta = self._request(f'files/{file_id}', {
            'fields': GET_PHOTO_META_FIELDS,
            'supportsAllDrives': 'true',
        })

        thumb_link = meta.get('thumbnailLink')
        mime = meta.get('mimeType')
        image_meta = meta.get('imageMediaMetadata') or {}

        # thumbnailLink が返す画像には EXIF Orientation タグが残らない（Drive 側で
        # 回転を適用済みの画素として返される）ことを実測で確認済み。もし将来 Drive の
        # 挙動が変わって Orientation が残るようになると、PhotoCache 側の原本モード
        # （self.originals=True）の exif_transpose が再度回転をかけてしまい二重回転になる
        # （photo_cache.py の `_encode_jpeg()` は `delivers_originals=True` の取得元に
        # 対して常に exif_transpose を試みる契約のため）。
        if thumb_link:
            req_w, req_h = self._required_thumbnail_size(image_meta)
            base = thumb_link.rsplit('=', 1)[0] if '=' in thumb_link else thumb_link
            size_spec = f'w{req_w}-h{req_h}' if req_w and req_h else 's2048'
            url = f'{base}={size_spec}'
            try:
                resp = requests.get(url, timeout=DEFAULT_TIMEOUT)
            except requests.exceptions.RequestException as e:
                logger.warning('thumbnailLink の取得に失敗しました: %s host=%s size=%s',
                              type(e).__name__, _redact(url), size_spec)
                resp = None
            if resp is not None:
                if resp.status_code == 200 and resp.content:
                    if self._looks_large_enough(resp.content, req_w, req_h):
                        return resp.content
                else:
                    logger.warning('thumbnailLink が %d を返しました: host=%s size=%s',
                                  resp.status_code, _redact(url), size_spec)

        if mime not in ORIGINAL_MIME_ALLOWED:
            logger.warning(
                'thumbnailLink を利用できず、原本もサポート外形式のためスキップします: id=%s mime=%s',
                file_id, mime)
            self._skipped_originals.add(file_id)
            return None

        logger.info('thumbnailLink が利用できないため原本を取得します: id=%s mime=%s', file_id, mime)
        return self._fetch_original(file_id)

    def fetch_album_thumbnail(self, album: dict[str, Any]) -> bytes | None:
        """
        フォルダを名前順に並べた先頭の画像（JPEG を優先）の `thumbnailLink` を
        `=s500` で取得する（`PhotoCache.store_thumbnail()` が短辺250pxへ縮小する）。
        """
        folder_id = album.get('id')
        if not folder_id:
            return None

        mime_filter = ' or '.join(f"mimeType='{m}'" for m in ALLOWED_IMAGE_MIMES)
        entries = self._list_folder(folder_id, mime_filter=mime_filter, fields=LIST_THUMB_FIELDS,
                                    order_by='name', page_size=10)
        if not entries:
            return None
        entries.sort(key=lambda e: (0 if e.get('mimeType') == 'image/jpeg' else 1, e.get('name') or ''))

        for entry in entries:
            thumb_link = entry.get('thumbnailLink')
            if not thumb_link:
                continue
            base = thumb_link.rsplit('=', 1)[0] if '=' in thumb_link else thumb_link
            url = f'{base}=s500'
            try:
                resp = requests.get(url, timeout=DEFAULT_TIMEOUT)
            except requests.exceptions.RequestException as e:
                logger.warning('アルバムサムネイルの取得に失敗しました: %s host=%s',
                              type(e).__name__, _redact(url))
                continue
            if resp.status_code == 200 and resp.content:
                return resp.content
        return None
