"""
ローカルフォルダを写真取得元として扱う PhotoProvider 実装

`LOCAL_PHOTO_ROOT`（未設定なら `/photos`、無ければ `./photos`）直下のサブフォルダを
アルバムとして扱い、ルート直下に画像が直接あれば「未分類」の仮想アルバムを
先頭へ1件差し込む（Drive 実装 `gdrive_api.py` と同じ構造）。

**写真はメモリへ読み込まず `Path` のまま `PhotoCache` へ渡す。** `PhotoCache` の
原本モード（`delivers_originals = True`）が EXIF 補正・画素上限・JPEG の draft
デコードを行うため、ここではファイルを開かない（撮影日の取得だけはヘッダの
EXIF を読むが、画素はデコードしない）。

**HEIC/HEIF は対象外。** 原本をデコードすると 48MP でピーク約602MB
（PoC 実測）になり、実機の RAM 416MB では成り立たないため、拡張子で除外して
件数だけログに残す（Drive と同じ方針。Drive は thumbnailLink の変換で救えるが、
ローカルには縮小済みの手段が無い）。

**ファイルは読み取り専用で扱い、書き込み・削除はしない。** 追加依存は無い。
"""

import base64
import binascii
import hashlib
import logging
import os
import threading
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image, UnidentifiedImageError

from src.photo_cache import original_pixel_limit
from src.photo_provider import ProviderError

logger = logging.getLogger(__name__)

# コンテナ内の既定のマウント先と、Dev Container（階層1）用のフォールバック。
# config_manager.py の PF_CONFIG_DIR と同じく、未設定でも階層1で相対パスのまま動く。
DEFAULT_CONTAINER_ROOT = '/photos'
DEFAULT_DEV_ROOT = './photos'

# 原本経路で扱ってよい拡張子（小文字）。HEIC/HEIF は含めない（モジュール冒頭参照）
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
SKIPPED_EXTENSIONS = ('.heic', '.heif')

# ルートを表すアルバム ID。フォルダ ID は必ず 'd.' か 'h.' で始まるので衝突しない
ROOT_ALBUM_ID = 'r'
COVER_ID = 'cover'

# PhotoCache._safe_name() は 120 文字で切るため、ID がそれを超えると異なる写真が
# 同じキャッシュファイルに衝突する。ID 全体を 120 文字以内に収める
MAX_ID_LEN = 120
# 相対パスを base64 で ID へ埋める上限（超えたらハッシュ＋対応表へ倒す）
MAX_EMBED_B64_LEN = 96

EXIF_IFD_POINTER = 0x8769
EXIF_DATETIME_ORIGINAL = 0x9003


def resolve_photo_root() -> Path:
    """ 写真ルートを解決する。LOCAL_PHOTO_ROOT > /photos（存在すれば） > ./photos """
    env = (os.environ.get('LOCAL_PHOTO_ROOT') or '').strip()
    if env:
        return Path(env)
    if Path(DEFAULT_CONTAINER_ROOT).is_dir():
        return Path(DEFAULT_CONTAINER_ROOT)
    return Path(DEFAULT_DEV_ROOT)


def _b64e(text: str) -> str:
    raw = text.encode('utf-8', 'surrogateescape')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64d(token: str) -> str:
    pad = '=' * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + pad).decode('utf-8', 'surrogateescape')


def _display(text: str) -> str:
    """
    表示・ログ用の名前へ直す。非 UTF-8 のファイル名は surrogateescape の孤立
    サロゲートを含み、そのまま写真リスト JSON や settings.json へ書くと
    UnicodeEncodeError で途中までしか書けない。ID（base64）は元のバイト列のまま
    なので、ここで置換文字にするのは表示名だけ。
    """
    return text.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')


def _sha(text: str, n: int) -> str:
    return hashlib.sha1(text.encode('utf-8', 'surrogateescape')).hexdigest()[:n]


class LocalFolderAPI:
    """
    ローカルフォルダを扱うクラス（`PhotoProvider` を満たす）。

    ID の設計（`PhotoCache` は ID を `_safe_name()`（英数字と `_.-` 以外を `_` へ、
    120 文字で切る）に通してファイル名にするため、ここで衝突・切り詰めを避ける）:

    - アルバム ID: ルート `'r'` / フォルダ `'d.<base64(フォルダ名)>'`。base64 が
      長すぎるときは `'h.<sha1先頭24文字>'`（解決は一覧を走査して照合する）
    - アセット ID: `'<sig8>.p<base64(相対パス)>'` または `'<sig8>.h<sha1先頭24文字>'`。
      `sig8` は mtime_ns とサイズのハッシュで、ファイルを差し替えるとキャッシュキーが
      変わる。ハッシュ形式の解決は、対応表（プロセス内）を引き、無ければ木を
      走査し直して作る（再起動後にキャッシュ済みの写真リストの ID を引くため。
      深さは高々2階層なので走査は軽い）
    """

    def __init__(self, settings_manager: Any = None,
                 status_callback: Callable[[str], None] | None = None,
                 root: str | Path | None = None) -> None:
        self.name = 'local'
        self.cache_namespace = 'local'
        self.supports_favorites = False
        self.delivers_originals = True
        # 表紙は名前順の先頭画像（'cover' 固定）。先頭が変わっても ID は変わらない
        # ため Drive と同じく期限で取り直す（photo_provider.py の Protocol 参照）
        self.album_thumbnail_expires = True
        # フォルダの中身はいつでも変わりうる。キャッシュ済みの写真リストが生きて
        # いても毎回走査し直す（ディレクトリ走査は安価で、通信も伴わない）
        self.rescan_on_load = True

        self.settings = settings_manager
        self._status_callback = status_callback

        self.root = Path(root) if root is not None else resolve_photo_root()
        if not self.root.is_dir():
            raise ValueError(
                f'ローカル写真フォルダが見つかりません: {self.root}'
                '（LOCAL_PHOTO_ROOT で指定するか、/photos へマウントしてください）')
        self._real_root = os.path.realpath(self.root)

        self._table_lock = threading.Lock()
        # ハッシュ形式の ID -> ルートからの相対パス（'/' 区切り）
        self._hash_paths: dict[str, str] = {}

    def update_status(self, message: str) -> None:
        """ 進捗を通知する。コールバック未指定でも実機の調査手段としてログには必ず残す """
        logger.info(message)
        if self._status_callback:
            self._status_callback(message)

    # ------------------------------------------------------------ パス・ID 補助

    def _inside_root(self, path: Path) -> bool:
        """ realpath がルート配下か（シンボリックリンク・'..' でルート外へ出ないため） """
        real = os.path.realpath(path)
        prefix = self._real_root if self._real_root.endswith(os.sep) else self._real_root + os.sep
        return real == self._real_root or real.startswith(prefix)

    def _scan_dir(self, directory: Path) -> tuple[list[Path], list[Path], int]:
        """
        directory 直下を走査し (サブフォルダ, 対応画像, スキップした HEIC 数) を名前順で返す。
        隠しエントリ（'.' 始まり）とルート外を指すものは無視する。OSError は呼び出し側へ。
        """
        folders: list[Path] = []
        images: list[Path] = []
        skipped = 0
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            if entry.name.startswith('.'):
                continue
            path = Path(entry.path)
            try:
                if entry.is_dir():
                    if self._inside_root(path):
                        folders.append(path)
                    continue
                if not entry.is_file():
                    continue
            except OSError:
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in SKIPPED_EXTENSIONS:
                skipped += 1
            elif ext in IMAGE_EXTENSIONS:
                if self._inside_root(path):
                    images.append(path)
                else:
                    logger.warning('ルート外を指すファイルを無視します: %s', _display(entry.name))
        return folders, images, skipped

    def _album_id_for(self, folder: Path) -> str:
        rel = folder.relative_to(self.root).as_posix()
        token = _b64e(rel)
        if len(token) <= MAX_EMBED_B64_LEN:
            return f'd.{token}'
        return f'h.{_sha(rel, 24)}'

    def _resolve_album_dir(self, album_id: str) -> Path | None:
        """ アルバム ID からフォルダを引く。ルート外・存在しない・不正な ID は None """
        if album_id == ROOT_ALBUM_ID:
            return self.root
        if album_id.startswith('d.'):
            try:
                rel = _b64d(album_id[2:])
            except (binascii.Error, UnicodeDecodeError, ValueError):
                return None
            # フォルダ名は1階層のみ。'/' や '..' を含む細工 ID はここで弾く
            if not rel or '/' in rel or '\\' in rel or rel in ('.', '..'):
                return None
            path = self.root / rel
        elif album_id.startswith('h.'):
            try:
                folders, _, _ = self._scan_dir(self.root)
            except OSError:
                return None
            matches = [f for f in folders if self._album_id_for(f) == album_id]
            if not matches:
                return None
            path = matches[0]
        else:
            return None
        if path.is_dir() and self._inside_root(path):
            return path
        return None

    @staticmethod
    def _signature(path: Path) -> str:
        st = path.stat()
        return _sha(f'{st.st_mtime_ns}:{st.st_size}', 8)

    def _asset_id_for(self, path: Path) -> str | None:
        try:
            sig = self._signature(path)
        except OSError:
            return None
        rel = path.relative_to(self.root).as_posix()
        token = _b64e(rel)
        if len(token) <= MAX_EMBED_B64_LEN:
            asset_id = f'{sig}.p{token}'
        else:
            digest = _sha(rel, 24)
            asset_id = f'{sig}.h{digest}'
            with self._table_lock:
                self._hash_paths[digest] = rel
        assert len(asset_id) <= MAX_ID_LEN
        return asset_id

    def _rebuild_hash_table(self) -> None:
        """ 対応表に無いハッシュ ID を引くため、深さ2の木を走査して作り直す """
        table: dict[str, str] = {}
        try:
            folders, images, _ = self._scan_dir(self.root)
            for folder in folders:
                images = images + self._scan_dir(folder)[1]
        except OSError as e:
            logger.warning('ローカルフォルダの走査に失敗しました: %s', e)
            return
        for path in images:
            rel = path.relative_to(self.root).as_posix()
            if len(_b64e(rel)) > MAX_EMBED_B64_LEN:
                table[_sha(rel, 24)] = rel
        with self._table_lock:
            self._hash_paths.update(table)

    def _resolve_asset(self, asset_id: str) -> Path | None:
        """
        アセット ID から現在のファイルを引く。ID の `sig8` が現物の mtime/size と
        食い違っても現物を返す（ファイルは差し替わっているが、次の走査で新しい ID の
        リストに置き換わる。ここで拒否すると表示が途切れるだけで得るものが無い）。
        """
        head, dot, tail = asset_id.partition('.')
        if not dot or not tail:
            return None
        if tail[0] == 'p':
            try:
                rel = _b64d(tail[1:])
            except (binascii.Error, UnicodeDecodeError, ValueError):
                return None
        elif tail[0] == 'h':
            digest = tail[1:]
            with self._table_lock:
                rel = self._hash_paths.get(digest)
            if rel is None:
                self._rebuild_hash_table()
                with self._table_lock:
                    rel = self._hash_paths.get(digest)
            if rel is None:
                return None
        else:
            return None

        path = self.root / rel
        if not self._inside_root(path):
            logger.warning('ルート外を指す ID を拒否しました: %s', asset_id[:40])
            return None
        if os.path.splitext(path.name)[1].lower() not in IMAGE_EXTENSIONS:
            return None
        if not path.is_file():
            return None
        try:
            if self._signature(path) != head:
                logger.debug('ファイルが差し替わっています（現物を返します）: %s', _display(rel))
        except OSError:
            return None
        return path

    @staticmethod
    def _read_date(path: Path) -> str:
        """
        EXIF の DateTimeOriginal だけを読む。無い・壊れている・読めないときは空文字。
        出力形式は gdrive_api の `_normalize_time` と同じ ISO（秒まで）。

        **`img.getexif()` を呼ばない。** Pillow 10.4 の PNG は `info` に 'exif' が無いと
        `getexif()` が `load()` で全画素をデコードする（階層1の実測: 6000x5000 の PNG で
        +117MB）。撮影日のためにそれをしないよう、ヘッダの時点で `info['exif']` に
        あるバイト列だけを `Exif.load()` で解析する（JPEG / WebP / PNG の eXIf が
        IDAT より前にあるもの。IDAT の後ろにある eXIf は読まず空文字になる）。
        画素上限（形式別。`original_pixel_limit`）を超える画像は開いたあとでも
        日付を読まない。Image.open() が投げる DecompressionBombError（OSError 系
        ではない）や警告で一覧全体を壊さないため、想定外の例外も握る。
        """
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', Image.DecompressionBombWarning)
                with Image.open(path) as img:
                    width, height = img.size
                    if width * height > original_pixel_limit(img.format):
                        return ''
                    raw = img.info.get('exif')
                    if not raw:
                        return ''
                    exif = Image.Exif()
                    exif.load(raw)
                    value = exif.get_ifd(EXIF_IFD_POINTER).get(EXIF_DATETIME_ORIGINAL)
        except Exception as e:  # noqa: BLE001 - 1枚の失敗で一覧を落とさない
            logger.debug('撮影日を読めませんでした: %s (%s)', _display(path.name), type(e).__name__)
            return ''
        if not value:
            return ''
        if isinstance(value, bytes):
            value = value.decode('ascii', 'ignore')
        try:
            return datetime.strptime(str(value).strip('\x00 '),
                                     '%Y:%m:%d %H:%M:%S').strftime('%Y-%m-%dT%H:%M:%S')
        except ValueError:
            return ''

    # ------------------------------------------------------ PhotoProvider Protocol

    def _check_root(self) -> None:
        """ ルートを読めなければ `ProviderError`（マウント切れ・権限・ルート自体の消失） """
        try:
            with os.scandir(self.root):
                pass
        except OSError as e:
            raise ProviderError(f'写真フォルダを読み込めません: {type(e).__name__}') from e

    def fetch_albums(self) -> list[dict[str, Any]]:
        """
        ルート直下のサブフォルダをアルバムとして返す（名前順）。ルート直下に
        対応画像があれば先頭に「未分類」仮想アルバムを差し込む。

        **ルートを読めないときは `ProviderError`**（空リストにすると、
        `photo_source` が「アルバムが1つも無い」と「読めなかった」を区別できず、
        マウント切れで失効キャッシュへ落とすべきところが空になる）。
        `album.py` のワーカーは例外を握って空リストとして扱う。
        """
        try:
            folders, images, _ = self._scan_dir(self.root)
        except OSError as e:
            self.update_status(f'アルバム取得エラー: {type(e).__name__}')
            raise ProviderError(f'写真フォルダを読み込めません: {type(e).__name__}') from e

        albums: list[dict[str, Any]] = [
            {'id': self._album_id_for(f), 'albumName': _display(f.name),
             'albumThumbnailAssetId': COVER_ID}
            for f in folders]
        if images:
            # albumName は空文字のまま保つ（album.py の name_key 契約。
            # _confirm_selection() が entry.get('albumName') or '' を保存するため、
            # 翻訳済み文字列が設定ファイルへ書かれない前提になる）
            albums.insert(0, {'id': ROOT_ALBUM_ID, 'albumName': '',
                              'name_key': 'album.local_root',
                              'albumThumbnailAssetId': COVER_ID})
        return albums

    def fetch_album_assets(self, album_id: str, album_name: str = '',
                           apply_album_order: bool = True) -> list[dict[str, Any]]:
        """
        フォルダ直下（再帰しない）の画像を名前順で返す。`apply_album_order` は
        フォルダに並び順設定が無いため無視する（Protocol 互換のためだけに残す）。

        **フォルダが無くなっていたら空リスト**（走査に成功して写真が無い、という
        正しい結果。`rescan_on_load` の取得元では `photo_source` が失効キャッシュへ
        落とさず空を採用する）。I/O エラー（ルートを読めない・マウント切れ等）だけ
        `ProviderError`（失効キャッシュへフォールバックさせる）。
        1枚の処理失敗（壊れたファイル等）は警告して読み飛ばし、他の写真は返す。
        """
        self._check_root()
        directory = self._resolve_album_dir(album_id)
        if directory is None:
            logger.info('アルバムのフォルダが見つかりません（空として扱います）: %s', album_id[:40])
            return []
        try:
            _, images, skipped = self._scan_dir(directory)
        except (FileNotFoundError, NotADirectoryError):
            logger.info('アルバムのフォルダが走査中に消えました（空として扱います）: %s',
                        album_id[:40])
            return []
        except OSError as e:
            raise ProviderError(f'フォルダを読み込めません: {type(e).__name__}') from e

        album_name = _display(album_name)
        if skipped:
            logger.info('HEIC/HEIF は原本をデコードしないためスキップしました: %d 件 (%s)',
                        skipped, album_name or album_id[:40])

        assets: list[dict[str, Any]] = []
        for path in images:
            try:
                asset_id = self._asset_id_for(path)
                if asset_id is None:
                    continue
                info: dict[str, Any] = {'id': asset_id, 'description': '',
                                        'date': self._read_date(path)}
            except Exception as e:  # noqa: BLE001 - 1枚の失敗で一覧を落とさない
                logger.warning('写真を読み飛ばしました: %s (%s)', _display(path.name),
                               type(e).__name__)
                continue
            if album_name:
                info['album_name'] = album_name
            assets.append(info)
        return assets

    def fetch_favorite_assets(self) -> list[dict[str, Any]]:
        """ ローカルフォルダはお気に入りに対応しない。呼ばれたら空リスト """
        logger.warning('ローカルフォルダはお気に入りに対応していません。呼び出しを無視します')
        return []

    def fetch_photo(self, asset_id: str) -> Path | None:
        """
        写真ファイルのパスを返す（メモリへは読み込まない。原本のデコードと縮小は
        `PhotoCache` が行う）。ファイルが消えた・ルート外なら None。
        """
        return self._resolve_asset(asset_id)

    def fetch_album_thumbnail(self, album: dict[str, Any]) -> Path | None:
        """ フォルダを名前順にした先頭の画像（JPEG を優先）のパスを返す """
        album_id = album.get('id')
        if not album_id:
            return None
        directory = self._resolve_album_dir(album_id)
        if directory is None:
            return None
        try:
            _, images, _ = self._scan_dir(directory)
        except OSError as e:
            logger.warning('アルバムサムネイル用の走査に失敗しました: %s', e)
            return None
        if not images:
            return None
        images.sort(key=lambda p: (0 if p.suffix.lower() in ('.jpg', '.jpeg') else 1, p.name))
        return images[0]
