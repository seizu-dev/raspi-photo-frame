import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image, ImageOps, UnidentifiedImageError

if TYPE_CHECKING:
    from src.config_manager import ConfigManager

logger = logging.getLogger(__name__)

# キャッシュディレクトリの既定値。コンテナでは Dockerfile の ENV が PF_CACHE_DIR=/cache を
# 与えるが、Dev Container（階層1）では未設定のままワークスペース相対で動く契約になっている。
DEFAULT_CACHE_DIR = './cache'
DEFAULT_DISPLAY_SIZE = (1024, 600)
JPEG_QUALITY = 90

# 写真の表示方法。設定画面・main.py はここから import して重複定義を避ける
# （gui/transitions.py の TRANSITION_VALUES と同じ扱い）。
FIT_CONTAIN = 'contain'  # 写真全体が収まる（余白あり）。現行の既定挙動
FIT_COVER = 'cover'      # 画面を隙間なく埋める（はみ出しは切り落とす）
FIT_SMART = 'smart'      # 写真の向きが画面の向きと一致するときだけ cover、それ以外は contain
FIT_VALUES: tuple[str, ...] = (FIT_CONTAIN, FIT_COVER, FIT_SMART)

# contain は既存キャッシュ（151MB / 676枚）を温存するため接尾辞を付けない。
# cover / smart だけ別名にして共存させ、使われなくなった方は enforce_limit() の
# LRU（mtime 昇順）で自然に消える。
_FIT_SUFFIXES: dict[str, str] = {FIT_CONTAIN: '', FIT_COVER: '__cover', FIT_SMART: '__smart'}

# normalize_fit() が同じ不正値について何度も警告を出さないための記録。
# モジュールレベルで持つのは、normalize_fit() の呼び出し側（PhotoCache 以外にも
# settings.py などが検証目的で呼びうる）ごとに集合を渡させる契約にしないため。
_warned_fit_values: set[str] = set()

# mtime を LRU の代用にする。relatime の環境では atime が更新されないため。
# SD カードの書き込みを抑えるため、前回更新からこの秒数が経つまで touch しない。
UTIME_MIN_INTERVAL_SEC = 3600

# 容量の実走査は保存のたびには行わない（数千ファイルの stat がスライド表示を妨げるため）
ENFORCE_EVERY_N_STORES = 20


def normalize_fit(value: Any) -> str:
    """
    設定値 `photo_fit` を FIT_VALUES のいずれかへ正規化する。不正値は FIT_CONTAIN。

    `ConfigManager.get()` は型を検証しないため、`settings.json` を手で壊すと
    文字列以外（list / dict / None / 数値など）が来うる。以前 `transition` で
    非文字列をそのまま警告記録用の `set` に入れて `TypeError: unhashable type` を
    起こし、`restart: unless-stopped` の実機で同じ値を読み直しては落ちる
    クラッシュループになった事故がある
    （.claude/context/known-issues.md「`transition` に文字列以外が入ると
    クラッシュループしていた」）。同じ穴を作らないよう、`FIT_VALUES` との
    比較より先に `isinstance` で文字列であることを確認する。
    """
    if isinstance(value, str) and value in FIT_VALUES:
        return value
    # 非文字列は set のキーにできないことがある（list / dict はハッシュ不可）ため、
    # 常にハッシュ可能な repr() へ変換してから記録する。
    warn_key = value if isinstance(value, str) else repr(value)
    if warn_key not in _warned_fit_values:
        _warned_fit_values.add(warn_key)
        logger.warning('未知の photo_fit 設定値です。contain として扱います: %r', value)
    return FIT_CONTAIN


def _orientation(width: int, height: int) -> int:
    """ 横長なら 1 / 縦長なら -1 / 正方形なら 0 を返す（純粋関数） """
    if width > height:
        return 1
    if width < height:
        return -1
    return 0


def resolve_fit(fit: str, image_size: tuple[int, int],
                display_size: tuple[int, int]) -> str:
    """
    smart を実画像の向きから contain / cover のどちらかへ解決する（純粋関数）。

    smart 以外はそのまま返す。判定は「写真の 縦 と 横 の大小関係」が
    「画面の 縦 と 横 の大小関係」と一致するかどうかで、1024x600（横長）を
    決め打ちにしない（解像度が変わっても壊れないようにするため）。
    正方形（大小関係が無い）は画面がどちらを向いていても一致しない扱いとし、
    contain に倒す（cover にすると常に中央を正方形に切り抜くことになり、
    「向きが合っている写真だけ埋める」という意図から外れるため）。
    """
    if fit != FIT_SMART:
        return fit
    img_w, img_h = image_size
    disp_w, disp_h = display_size
    if img_w <= 0 or img_h <= 0 or disp_w <= 0 or disp_h <= 0:
        return FIT_CONTAIN
    img_orientation = _orientation(img_w, img_h)
    disp_orientation = _orientation(disp_w, disp_h)
    if img_orientation != 0 and img_orientation == disp_orientation:
        return FIT_COVER
    return FIT_CONTAIN


def resolve_cache_dir() -> Path:
    """ キャッシュディレクトリを解決する。PF_CACHE_DIR が未設定なら ./cache へフォールバックする """
    return Path(os.environ.get('PF_CACHE_DIR') or DEFAULT_CACHE_DIR)


def _safe_name(value: Any) -> str:
    """ 外部由来の文字列をファイル名に使える形へ落とす（パストラバーサル対策） """
    return re.sub(r'[^A-Za-z0-9_.-]', '_', str(value))[:120] or '_'


class PhotoCache:
    """
    表示解像度で確定済みの画像をディスクにキャッシュするクラス

    photo-frame の cache_manager.py と thumbnail_cache_manager.py を統合した再設計版。
    実行時のリサイズを完全に排除するため、ダウンロード時点で表示解像度へ確定させて保存する。

    写真本体はアセット単位でフラットに置き、アルバムをまたいで共有する。
    同じ写真が複数のアルバムに含まれていても実体は1つで済み、写真ソースを切り替えても
    再ダウンロードが発生しない（2.4GHz Wi-Fi の帯域を浪費しないため）。
    """

    def __init__(self, config_manager: 'ConfigManager', cache_dir: str | Path | None = None,
                 display_size: tuple[int, int] = DEFAULT_DISPLAY_SIZE) -> None:
        self.config = config_manager
        self.cache_dir = Path(cache_dir) if cache_dir else resolve_cache_dir()
        self.display_size = display_size

        self.photos_dir = self.cache_dir / 'photos'
        self.lists_dir = self.cache_dir / 'lists'
        self.thumbs_dir = self.cache_dir / 'thumbs'
        for d in (self.photos_dir, self.lists_dir, self.thumbs_dir):
            d.mkdir(parents=True, exist_ok=True)

        # 先読みスレッドとメインスレッドから同時に呼ばれるため、削除処理だけは直列化する
        self._lock = threading.Lock()
        self._store_count = 0

    # ------------------------------------------------------------------ 写真本体

    def _resolve_fit_setting(self) -> str:
        """ 現在の photo_fit 設定値を読んで正規化する（_photo_path / store_photo が共有する） """
        return normalize_fit(self.config.get('photo_fit', FIT_CONTAIN))

    def _photo_path(self, asset_id: str, fit: str | None = None) -> Path:
        """
        1ディレクトリあたりのファイル数を抑えるため先頭2文字で分割する。

        方式ごとに別ファイル名にして共存させる（contain は接尾辞なしのまま）。
        `fit` を渡さない呼び出し（get_photo_path 等）は現在の設定値をその場で読む。
        store_photo() は _encode_jpeg() と同じ方式で揃えるため、解決済みの値を渡す。
        """
        if fit is None:
            fit = self._resolve_fit_setting()
        name = _safe_name(asset_id)
        suffix = _FIT_SUFFIXES[fit]
        return self.photos_dir / name[:2] / f'{name}{suffix}.jpg'

    def get_photo_path(self, asset_id: str) -> Path | None:
        """ キャッシュ済みの写真のパスを返す。無ければ None """
        path = self._photo_path(asset_id)
        if not path.exists():
            return None
        self._touch(path)
        return path

    def store_photo(self, asset_id: str, raw: bytes) -> Path | None:
        """
        ダウンロードした画像を表示解像度へ確定させて保存する。

        ここでリサイズ（と cover の切り抜き）を済ませるため、描画パスでは
        リサイズが一切発生しない。方式は呼び出しの間で変わりうるため、
        ここで一度だけ解決してエンコードとパスの両方に渡し、
        ズレ（違う方式でエンコードしたのに別の方式のファイル名で保存する等）を防ぐ。
        """
        fit = self._resolve_fit_setting()
        data = self._encode_jpeg(raw, self.display_size, fit)
        if data is None:
            return None

        path = self._photo_path(asset_id, fit)
        if not self._write_atomic(path, data):
            return None

        logger.info('写真をキャッシュしました: %s (%d KB)', asset_id, len(data) // 1024)
        self.enforce_limit()
        return path

    # ------------------------------------------------------------ 写真リスト（JSON）

    def _list_path(self, key: str) -> Path:
        return self.lists_dir / f'{_safe_name(key)}.json'

    def get_asset_list(self, key: str,
                       ignore_lifetime: bool = False) -> list[dict[str, Any]] | None:
        """
        キャッシュ済みの写真リストを返す。失効していれば None。

        ネットワーク断はキャッシュへフォールバックする正常系として扱うため、
        呼び出し側は None のときだけ API を叩けばよい。

        ignore_lifetime=True は「API も失敗した」ときの最後の砦。
        古くてもリストがあれば写真を出し続けたいので、失効判定を飛ばして返す。
        """
        path = self._list_path(key)
        if not path.exists():
            return None

        try:
            with open(path, 'r', encoding='utf-8') as f:
                payload = json.load(f)
            cached_at = datetime.fromisoformat(payload['cached_at'])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning('写真リストのキャッシュを読めません: %s (%s)', path, e)
            return None

        lifetime = timedelta(hours=self.config.get('cache_lifetime_hours', 24))
        if not ignore_lifetime and datetime.now() - cached_at > lifetime:
            logger.info('写真リストのキャッシュが失効しています: %s', key)
            return None

        assets = payload.get('assets')
        return assets if isinstance(assets, list) else None

    def store_asset_list(self, key: str, assets_info: list[dict[str, Any]]) -> bool:
        """ 写真リストを保存する。オフライン時の表示継続に使う """
        payload = {'cached_at': datetime.now().isoformat(), 'assets': assets_info}
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        if not self._write_atomic(self._list_path(key), data):
            return False
        logger.info('写真リストをキャッシュしました: %s (%d 件)', key, len(assets_info))
        return True

    # -------------------------------------------------------- アルバムサムネイル

    def _thumbnail_path(self, album_id: str, thumbnail_id: str) -> Path:
        return self.thumbs_dir / f'{_safe_name(album_id)}_{_safe_name(thumbnail_id)}.jpg'

    def get_thumbnail_path(self, album_id: str, thumbnail_id: str) -> Path | None:
        """
        アルバムサムネイルのパスを返す。

        サムネイルの差し替えでアセット ID が変わることがあるため、
        同じアルバムの古いファイルはここで掃除する。
        """
        if not album_id or not thumbnail_id:
            return None

        path = self._thumbnail_path(album_id, thumbnail_id)
        if path.exists():
            return path

        for old in self.thumbs_dir.glob(f'{_safe_name(album_id)}_*.jpg'):
            if old != path:
                self._unlink(old, '古いサムネイル')
        return None

    def store_thumbnail(self, album_id: str, thumbnail_id: str, raw: bytes) -> Path | None:
        """
        アルバムサムネイルを保存する。

        Immich のサムネイルは WebP で返ることがあるため、拡張子と中身を一致させる目的で
        JPEG へ変換する。既に小さいのでリサイズはしない。
        """
        if not album_id or not thumbnail_id:
            return None

        data = self._encode_jpeg(raw, None)
        if data is None:
            return None

        path = self._thumbnail_path(album_id, thumbnail_id)
        return path if self._write_atomic(path, data) else None

    def cleanup_thumbnails(self, active_albums: list[dict[str, Any]]) -> int:
        """ 現存するアルバムに対応しないサムネイルを削除する """
        active = set()
        for album in active_albums:
            album_id, thumb_id = album.get('id'), album.get('albumThumbnailAssetId')
            if album_id and thumb_id:
                active.add(self._thumbnail_path(album_id, thumb_id).name)

        removed = 0
        for path in self.thumbs_dir.glob('*.jpg'):
            if path.name not in active and self._unlink(path, '未使用のサムネイル'):
                removed += 1
        if removed:
            logger.info('未使用のサムネイルを %d 件削除しました', removed)
        return removed

    def cleanup_list_cache(self, prefix: str, keep_key: str) -> int:
        """
        同じ接頭辞を持つ写真リストのうち、keep_key 以外を削除する。

        デイリーピックアップはキャッシュキーに日付を含む（`daily_pickup_<日付>`）ため、
        掃除しないと日付ごとに JSON が積み上がる。過去日付のものは
        `cache_key()` が二度とその名前を作らないので参照されることがない。

        **どのキーが日付を含むかはここでは判断しない。** 接頭辞と残すキーを
        受け取るだけにして、キーの意味は呼び出し側（`photo_source.py`）に留める。
        """
        keep_name = self._list_path(keep_key).name
        safe_prefix = _safe_name(prefix)

        removed = 0
        for path in self.lists_dir.glob(f'{safe_prefix}*.json'):
            if path.name != keep_name and self._unlink(path, '過去の写真リスト'):
                removed += 1
        if removed:
            logger.info('過去の写真リストを %d 件削除しました（接頭辞 %s）', removed, prefix)
        return removed

    # ---------------------------------------------------------------- 容量管理

    def enforce_limit(self, force: bool = False) -> int:
        """
        写真本体の総容量が上限を超えていたら、古いものから削除する。

        走査コストがあるため、保存のたびではなく ENFORCE_EVERY_N_STORES 回ごとに実行する。
        force=True で即時実行する（起動時など）。
        """
        with self._lock:
            if not force:
                self._store_count += 1
                if self._store_count < ENFORCE_EVERY_N_STORES:
                    return 0
                self._store_count = 0

            limit_mb = int(self.config.get('photo_cache_max_mb', 512) or 0)
            if limit_mb <= 0:
                return 0
            limit = limit_mb * 1024 * 1024

            entries, total = [], 0
            for path in self.photos_dir.rglob('*.jpg'):
                try:
                    st = path.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, path))
                total += st.st_size

            if total <= limit:
                return 0

            entries.sort()  # mtime 昇順 = 最後に使われたのが古い順
            removed = 0
            for _, size, path in entries:
                if total <= limit:
                    break
                if self._unlink(path, 'キャッシュ上限超過'):
                    total -= size
                    removed += 1

            logger.info('キャッシュ上限 %dMB を超えたため %d 件削除しました（残 %.1fMB）',
                        limit_mb, removed, total / 1024 / 1024)
            return removed

    def get_total_size(self) -> int:
        """ 写真本体の総バイト数を返す（設定画面での表示用） """
        total = 0
        for path in self.photos_dir.rglob('*.jpg'):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def clear_all(self) -> None:
        """ すべてのキャッシュを削除する """
        import shutil
        with self._lock:
            for d in (self.photos_dir, self.lists_dir, self.thumbs_dir):
                shutil.rmtree(d, ignore_errors=True)
                d.mkdir(parents=True, exist_ok=True)
        logger.info('キャッシュをすべて削除しました')

    # ------------------------------------------------------------------ 内部処理

    def _encode_jpeg(self, raw: bytes, max_size: tuple[int, int] | None,
                     fit: str = FIT_CONTAIN) -> bytes | None:
        """
        画像を JPEG バイト列へ変換する。max_size が指定されていれば表示解像度へ確定させる。

        Immich の preview は JPEG と WebP の両方が返るため、フォーマットを前提にしない。
        `fit` は max_size 指定時（写真本体）にのみ意味を持つ。サムネイル保存
        （max_size=None、store_thumbnail() 経由）では参照されず、常に現行どおり
        「収める」経路（実質 thumbnail() すら通らない no-op）のままになる。
        """
        try:
            with Image.open(BytesIO(raw)) as img:
                if max_size and img.format == 'JPEG':
                    # draft() は JPEG のみ有効。1/2・1/4 スケールで直接デコードして
                    # デコード負荷とピークメモリを削る。他形式では何も起きない。
                    # cover が必要とする切り抜き元（幅 ≥ max_size 幅 かつ
                    # 高さ ≥ max_size 高さ）は draft() の「要求サイズ以上に
                    # デコードする」という保証でそのまま満たされるため、
                    # cover 用に別の縮小率へ変える必要はない。
                    img.draft('RGB', max_size)

                if max_size:
                    resolved_fit = resolve_fit(fit, img.size, max_size)
                    if resolved_fit == FIT_COVER:
                        # ImageOps.fit() は「元画像側で切り抜き範囲を決めてから
                        # 1回の resize で目的サイズを出す」実装になっている。
                        # 「まず crop() してから thumbnail() で拡縮する」素直な
                        # 2段実装に戻すと、パノラマのような巨大画像で
                        # crop 後の中間画像がまだ大きいままメモリに乗り、
                        # ピークメモリが膨らむ（RAM 512MB が最大の制約のため
                        # ここは必ず1回の resize で済ませること）。
                        # thumbnail() と異なり、元が max_size より小さい場合は
                        # 拡大される（「隙間なく埋める」以上そうなる仕様どおりの
                        # 非対称。小さい写真では contain と解像感が変わる）。
                        img = ImageOps.fit(img, max_size, method=Image.Resampling.LANCZOS,
                                           centering=(0.5, 0.5))
                    else:
                        # thumbnail() はアスペクト比を保ち、元より大きくはしない
                        img.thumbnail(max_size, Image.Resampling.LANCZOS)

                # RGBA / P モードのままでは JPEG で保存できない
                rgb = img if img.mode == 'RGB' else img.convert('RGB')
                buf = BytesIO()
                try:
                    rgb.save(buf, format='JPEG', quality=JPEG_QUALITY)
                finally:
                    if rgb is not img:
                        rgb.close()
                return buf.getvalue()
        except (UnidentifiedImageError, OSError, ValueError) as e:
            # 壊れたデータや未対応形式は握ってスキップする（1枚のために停止させない）
            logger.warning('画像を変換できませんでした: %s', e)
            return None

    def _write_atomic(self, path: Path, data: bytes) -> bool:
        """
        一時ファイルへ書いてから置き換える。

        電源断がありうる運用のため、書きかけのファイルをキャッシュとして残さない。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, path)
            return True
        except OSError as e:
            logger.warning('キャッシュを書き込めませんでした: %s (%s)', path, e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _touch(self, path: Path) -> None:
        """ LRU 用に mtime を更新する。書き込み回数を抑えるため間隔を空ける """
        try:
            if time.time() - path.stat().st_mtime >= UTIME_MIN_INTERVAL_SEC:
                os.utime(path, None)
        except OSError as e:
            logger.debug('mtime を更新できませんでした: %s (%s)', path, e)

    def _unlink(self, path: Path, reason: str) -> bool:
        try:
            path.unlink()
            logger.debug('%s を削除: %s', reason, path.name)
            return True
        except OSError as e:
            logger.warning('%s を削除できませんでした: %s (%s)', reason, path, e)
            return False
