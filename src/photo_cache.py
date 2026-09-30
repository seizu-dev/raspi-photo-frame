import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta
from contextlib import ExitStack
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

# 原本を扱う取得元（`self.originals=True`。Google Drive の原本経路）の写真本体だけに
# 使う保存設定。既定（Immich）の quality=90・subsampling 未指定（Pillow 既定 4:2:0）は
# 変えない（save() の引数を増やすと既存キャッシュとバイト単位で変わりうるため）。
# 4:2:0 の色差間引きがイラストの線まわりに色にじみ・ノイズを出していたため、
# 原本モードの写真本体だけ quality=95 / 4:4:4（subsampling=0）へ上げる。
# 階層1の実測（原本から LANCZOS 縮小した基準との PSNR、取得 `=w2048-h1200`）:
# イラスト 30.7dB（q90/4:2:0）→ 33.9dB（q95/4:4:4）、写真 34.2 → 38.6dB。
# キャッシュ1枚は約1.75倍（88KB→155KB）に増えるが、展開後のテクスチャのメモリは不変
# （どちらも JPEG をデコードすれば同じ 1024x600x4 の RGBA になるため）。
ORIGINALS_JPEG_QUALITY = 95
ORIGINALS_JPEG_SUBSAMPLING = 0

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

# アルバムサムネイルの短辺上限。Immich のサムネイルは既に 333x250 / 444x250 程度で
# 短辺がちょうど 250px のため、この値では縮小されない（変わらないことを実測で確認済み）。
# Drive 等（PR2）は縮小前のサムネイルを返しうるため、短辺がこれを超える場合だけ
# 縮小する（拡大はしない・アスペクト比は維持する）。
THUMBNAIL_MAX_SHORT_SIDE = 250

# 原本を扱う取得元（`delivers_originals = True`。Google Drive の原本経路・
# ローカルフォルダ）の画素上限。形式ごとに変える。JPEG は draft()（1/2〜1/8 の
# 縮小デコード）が効いてピークが画素数に比例しないが、PNG / WebP には縮小デコードが
# 無く、全画素をデコードするため上限を厳しくする（階層1の実測: 30MP の PNG で
# +129MB、WebP で +463MB）。
# HEIC の原本を無条件にデコードすると 48MP で maxrss 約602MB（PoC 実測）になり、
# RAM 416MB の実機では成り立たない。超過分はデコードせずスキップする
# （`.claude/architecture.md`「画素上限なしで原本をデコードしない」）。
#
# PNG / WebP の値は実機（arm64、v1.3.0）の実測に基づく。通常の経路（Pillow の
# Image.load）のピークは WebP 8MP で +121MB、4MP で +65MB、PNG 12MP で +61MB、
# 8MP で +41MB、JPEG 40MP（draft あり）で +17MB だった。Pillow 10.4.0 の WebP は
# 静止画でも WebPAnimDecoder 経由でデコードし、同じ画像を複数回複製するため
# 約15MB/MP と重い。`_decode_webp_direct()` はこれを `PIL._webp.WebPDecode` で避ける
# （階層1の実測、store_photo・静止 RGB・bytes 入力で 8MP: +127MB -> +65MB。
# 最悪ケースの RGBA + Orientation 6 の 8MP は +78MB（旧 +177MB））。**新経路の実機値は
# 未測定**（v1.3.1 で測定予定）。アニメーション WebP と、WebPDecode が使えない
# 場合は通常経路（+137MB 相当）になるため、画素上限を 4MP へ下げて掛ける。
# `MAX_ORIGINAL_PIXELS` は JPEG の上限を指す名前として従来どおり残す。
MAX_ORIGINAL_PIXELS = 40_000_000
MAX_ORIGINAL_PIXELS_PNG = 12_000_000
MAX_ORIGINAL_PIXELS_WEBP = 8_000_000
# WebPDecode が使えない環境（通常経路しか無い）での WebP の上限。実機の実測で
# 4MP が +65MB に収まるため、8MP（+121MB）は許さない。
MAX_ORIGINAL_PIXELS_WEBP_FALLBACK = 4_000_000

# `PIL._webp.WebPDecode` は Pillow の非公開 API。Pillow は requirements.txt で固定
# （10.4.0）しているが、将来の更新で消えうるため、無ければ通常経路へ戻し、
# 上限も厳しい側に倒す（判定はモジュール読み込み時に1回）。
try:
    from PIL import _webp as _pil_webp
    _WEBP_DIRECT_AVAILABLE = callable(getattr(_pil_webp, 'WebPDecode', None))
except ImportError:
    _pil_webp = None
    _WEBP_DIRECT_AVAILABLE = False


def original_pixel_limit(image_format: str | None) -> int:
    """
    形式（`Image.format`）ごとの原本の画素上限を返す。JPEG は 40MP、WebP は 8MP
    （`WebPDecode` が使えない環境では 4MP）、それ以外（PNG を含む。縮小デコードの
    無い形式）は PNG と同じ値に倒す。
    `local_api.py` も撮影日の読み取り可否の判定にこれを使う（式を複製しない）。
    """
    if image_format == 'JPEG':
        return MAX_ORIGINAL_PIXELS
    if image_format == 'WEBP':
        return (MAX_ORIGINAL_PIXELS_WEBP if _WEBP_DIRECT_AVAILABLE
                else MAX_ORIGINAL_PIXELS_WEBP_FALLBACK)
    return MAX_ORIGINAL_PIXELS_PNG


_WEBP_OK, _WEBP_SKIP, _WEBP_UNAVAILABLE = 'ok', 'skip', 'unavailable'


def _decode_webp_direct(raw: 'bytes | Path') -> 'tuple[str, Image.Image | None]':
    """
    静止画の WebP を `PIL._webp.WebPDecode` で直接デコードする。戻り値は
    (状態, 画像)。状態は次のいずれか。

    - `_WEBP_OK`: デコードできた（画像あり）。
    - `_WEBP_SKIP`: この1枚だけ諦める（壊れたデータ・MemoryError・読み込み失敗）。
      API 自体は正常なので、呼び出し側は通常経路へ落とさずスキップする
      （通常経路は約15MB/MP で、MemoryError になるような状況では落とす意味が無い）。
    - `_WEBP_UNAVAILABLE`: 非公開 API の形が想定と違う（属性なし・戻り値の形の不一致等）。
      呼び出し側が通常経路へ戻り、以後は使わない。

    Pillow 10.4.0 の WebPImagePlugin は静止画でも WebPAnimDecoder を通し、
    libwebp のキャンバス -> get_next() の bytes -> Pillow バッファと同じ画像を
    複数回複製するため約15MB/MP になる。WebPDecode は1回のデコードで済む。
    返す画像は EXIF を持たない（向きの補正は呼び出し側が行う）。
    """
    try:
        data = raw.read_bytes() if isinstance(raw, Path) else raw
    except (OSError, MemoryError) as e:
        logger.warning('WebP を読み込めませんでした: %s', e)
        return _WEBP_SKIP, None
    try:
        result = _pil_webp.WebPDecode(data)
    except MemoryError as e:
        logger.warning('WebP のデコード中にメモリが足りませんでした: %s', e)
        return _WEBP_SKIP, None
    except Exception as e:  # 非公開 API のため、呼び出し形の不一致等は「使えない」と見なす
        logger.warning('WebPDecode を使えないため通常のデコードに戻します: %s', e)
        return _WEBP_UNAVAILABLE, None
    del data
    if result is None:
        # 壊れたデータでは None が返る（API の異常ではない）
        logger.warning('WebP をデコードできませんでした（壊れたデータ）')
        return _WEBP_SKIP, None
    try:
        pixels, width, height, mode, _icc, _exif = result
        del result
        # RGB はコピーして作られるのでここで pixels を手放せる。RGBA はゼロコピーで
        # 参照するが、Pillow 内部が参照を持つため pixels を手放しても画素は生きている
        # （実測で確認）。
        im = Image.frombuffer(mode, (width, height), pixels, 'raw', mode, 0, 1)
        del pixels
    except MemoryError as e:
        logger.warning('WebP のデコード中にメモリが足りませんでした: %s', e)
        return _WEBP_SKIP, None
    except (TypeError, ValueError, AttributeError) as e:
        logger.warning('WebPDecode の戻り値が想定と違うため通常のデコードに戻します: %s', e)
        return _WEBP_UNAVAILABLE, None
    return _WEBP_OK, im



# プロセス全体で1本のデコードロック。原本を扱う取得元（Google Drive 等。PR2 で追加）は
# HEIC 等のデコード前サイズが大きい画像をそのまま開くことがあり、先読みスレッドと
# アルバムサムネイル取得スレッドが同時にデコードするとピークメモリが跳ね上がる
# （RAM 512MB が最大の制約のため、デコード〜エンコードの区間はプロセス全体で
# 直列化してピークを抑える。Immich の preview/thumbnail はそこまで大きくないが、
# 取得元によらず同じ経路を通るここで一律に直列化する）。
# RLock なのは、WebPDecode が使えないと分かったときに同じスレッドが
# `_encode_jpeg()` を通常経路でやり直す（再入する）ため。
_DECODE_LOCK = threading.RLock()


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
                 display_size: tuple[int, int] = DEFAULT_DISPLAY_SIZE,
                 namespace: str = '', originals: bool = False) -> None:
        """
        `namespace` は取得元ごとにキャッシュのサブディレクトリを分けるための識別子
        （`PhotoProvider.cache_namespace`。.claude/architecture.md「対で更新が必要な
        箇所」参照）。**空文字（Immich）は従来どおり `photos/` 等の直下を使う**ため、
        既存キャッシュのパスは1つも変わらない。空文字以外を渡すと
        `photos/<namespace>/` のようにサブディレクトリへ分離される。

        `originals` は取得元が `delivers_originals = True`（PhotoProvider Protocol）
        かどうかを表す。True のときだけ `_encode_jpeg()` が原本向けの処理
        （EXIF Orientation に応じた draft サイズの補正・`exif_transpose`・画素上限）を
        行う。**Immich 経路（False）には一切掛けない**（Immich の preview は
        既に正しい向きで返るため、二重回転の恐れがある）。
        """
        self.config = config_manager
        self.cache_dir = Path(cache_dir) if cache_dir else resolve_cache_dir()
        self.display_size = display_size
        self.namespace = namespace
        self.originals = originals

        # 容量の上限（enforce_limit / get_total_size）は取得元をまたいで1つ。
        # namespace を持つインスタンスでも、このルートを rglob して全取得元ぶんを
        # 合算する（.claude/architecture.md「対で更新が必要な箇所」参照）。
        self._photos_root = self.cache_dir / 'photos'

        ns_parts = (_safe_name(namespace),) if namespace else ()
        self.photos_dir = self._photos_root.joinpath(*ns_parts)
        self.lists_dir = self.cache_dir.joinpath('lists', *ns_parts)
        self.thumbs_dir = self.cache_dir.joinpath('thumbs', *ns_parts)
        for d in (self.photos_dir, self.lists_dir, self.thumbs_dir):
            d.mkdir(parents=True, exist_ok=True)

        # 先読みスレッドとメインスレッドから同時に呼ばれるため、削除処理だけは直列化する
        self._lock = threading.Lock()
        # 保存回数のカウンタと走査スレッドの管理は _lock とは別のロックで守る。
        # _lock は走査の間ずっと保持されるため、同じロックでカウンタを触ると
        # 保存したワーカーが走査の終了を待たされてしまう。
        self._count_lock = threading.Lock()
        self._store_count = 0
        self._enforce_thread: threading.Thread | None = None

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

    def store_photo(self, asset_id: str, raw: bytes | Path) -> Path | None:
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
        self._schedule_enforce()
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

    def is_thumbnail_stale(self, album_id: str, thumbnail_id: str, max_age_hours: float) -> bool:
        """
        サムネイルが `max_age_hours` より古いかを判定する（`album_thumbnail_expires`
        が True の取得元向け。`.claude/architecture.md`「対で更新が必要な箇所」参照）。

        判定は mtime（`os.replace()` による書き込み時刻）で行う。`get_thumbnail_path()`
        は `_touch()`（LRU 用の mtime 更新）を呼ばない契約なので、mtime は
        「最後に `store_thumbnail()` した時刻」のまま保たれる（`get_photo_path()` が
        `_touch()` を呼ぶ写真本体とは異なる）。ファイルが存在しない場合は
        「古い」とはみなさない（作り直しを急かす理由が無く、通常の初回取得の経路に任せる）。
        """
        if not album_id or not thumbnail_id or max_age_hours <= 0:
            return False
        path = self._thumbnail_path(album_id, thumbnail_id)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        return time.time() - mtime > max_age_hours * 3600

    def store_thumbnail(self, album_id: str, thumbnail_id: str, raw: bytes | Path) -> Path | None:
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
        force=True で即時実行する（起動時など）。どちらも呼び出したスレッドで同期的に
        実行し、削除件数を返す。保存経路（store_photo）は同期実行を避けるため
        `_schedule_enforce()` を使う。
        """
        if not force:
            with self._count_lock:
                self._store_count += 1
                if self._store_count < ENFORCE_EVERY_N_STORES:
                    return 0
                self._store_count = 0
        return self._scan_and_trim()

    def _schedule_enforce(self) -> None:
        """
        保存回数を数え、N 回に達したら走査を背景のデーモンスレッドで実行する。

        store_photo() は先読みワーカーの戻り道にあり、ここで同期的に rglob + stat を
        回すと、走査が終わるまで先読み結果がキューに入らず自動送りが見送られ続ける
        （実機では保存20回目・40回目の直後に 74〜92 秒止まった。メモリ圧迫で
        dentry キャッシュが追い出され、stat が SD カードへ行くため）。
        「写真をキャッシュしました」のログは走査より前に出るので、ログ上は
        「キャッシュ済みなのに送られない」ように見えていた。
        走査が実行中なら新たに起動しない（重複させない）。
        """
        with self._count_lock:
            self._store_count += 1
            if self._store_count < ENFORCE_EVERY_N_STORES:
                return
            self._store_count = 0
            if self._enforce_thread is not None and self._enforce_thread.is_alive():
                return
            thread = threading.Thread(target=self._enforce_worker,
                                      name='cache-enforce', daemon=True)
            self._enforce_thread = thread
            thread.start()

    def _enforce_worker(self) -> None:
        """ 背景走査の本体。例外でスレッドが黙って死なないよう握ってログに残す """
        try:
            self._scan_and_trim()
        except Exception:
            # 次の N 回後に再試行されるだけなので、ログは1回の走査につき1件に留まる
            logger.exception('キャッシュ上限の確認に失敗しました')

    def _scan_and_trim(self) -> int:
        """ 全体を走査し、上限超過ぶんを LRU で削除する（_lock で直列化） """
        with self._lock:
            limit_mb = int(self.config.get('photo_cache_max_mb', 512) or 0)
            if limit_mb <= 0:
                return 0
            limit = limit_mb * 1024 * 1024

            entries, total = [], 0
            for path in self._photos_root.rglob('*.jpg'):
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
        """ 写真本体の総バイト数を返す（設定画面での表示用。取得元をまたいで合算する） """
        total = 0
        for path in self._photos_root.rglob('*.jpg'):
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

    @staticmethod
    def _shrink(img: Image.Image, max_size: tuple[int, int] | None, fit: str,
                rotated: bool = False) -> Image.Image:
        """
        表示解像度（max_size 指定あり）または短辺 THUMBNAIL_MAX_SHORT_SIDE（なし）へ縮める。

        `rotated` は img が「90°回す前の軸」のときに True（WebP の直接デコード経路。
        縮小してから回転するため、fit の判定と目標寸法は見た目の向きで行い、
        目標だけ回転前の軸へ入れ替える）。cover の中央クロップは 90°回転・反転と
        可換なので、見た目の結果は「回転してから縮小」と同じになる。
        """
        eff_size = (img.height, img.width) if rotated else img.size
        eff_box = max_size
        if rotated and max_size:
            max_size = (max_size[1], max_size[0])
        if max_size:
            # fit の判定は見た目の向き（eff_size と元の max_size）で行う
            resolved_fit = resolve_fit(fit, eff_size, eff_box)
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
        else:
            # サムネイル保存経路（store_thumbnail() 経由）。Immich のサムネイルは
            # 既に短辺 250px 程度（333x250 / 444x250）のためここでは縮小されない
            # （寸法が変わらないことを実測で確認済み）。Drive 等（PR2）が
            # 縮小前のサムネイルを返す場合に備え、短辺が上限を超えるときだけ
            # 縮小する（拡大はしない・アスペクト比は維持する）。
            img_w, img_h = img.size
            short_side = min(img_w, img_h)
            if short_side > THUMBNAIL_MAX_SHORT_SIDE:
                scale = THUMBNAIL_MAX_SHORT_SIDE / short_side
                img = img.resize(
                    (max(1, round(img_w * scale)), max(1, round(img_h * scale))),
                    Image.Resampling.LANCZOS)
        return img

    def _shrink_then_rotate(self, img: Image.Image, orientation: Any, max_size: tuple[int, int] | None,
                            fit: str) -> Image.Image:
        """
        WebP の直接デコード結果を「縮小 -> 回転」の順で仕上げる。

        直接デコードした画像は EXIF を持たず `exif_transpose` が使えないうえ、
        先に回転すると原寸のコピーがもう1枚できてピークが跳ね上がる
        （RGBA + Orientation 6 の 8MP で +116MB）。そのため小さくしてから回す。
        対応表は Pillow の `ImageOps.exif_transpose` と同一。
        """
        method = {
            2: Image.Transpose.FLIP_LEFT_RIGHT,
            3: Image.Transpose.ROTATE_180,
            4: Image.Transpose.FLIP_TOP_BOTTOM,
            5: Image.Transpose.TRANSPOSE,
            6: Image.Transpose.ROTATE_270,
            7: Image.Transpose.TRANSVERSE,
            8: Image.Transpose.ROTATE_90,
        }.get(orientation)
        img = self._shrink(img, max_size, fit, rotated=orientation in (5, 6, 7, 8))
        if method is not None:
            img = img.transpose(method)
        return img

    def _encode_jpeg(self, raw: bytes | Path, max_size: tuple[int, int] | None,
                     fit: str = FIT_CONTAIN) -> bytes | None:
        """
        画像を JPEG バイト列へ変換する。max_size が指定されていれば表示解像度へ確定させる。

        `raw` は `bytes`（Immich 等、原本をメモリへ受け取る取得元）と `Path`
        （原本をファイルへストリーミング保存する取得元。Drive 原本経路向け）の
        両方を受け付ける。Immich の preview は JPEG と WebP の両方が返るため、
        フォーマットを前提にしない。`fit` は max_size 指定時（写真本体）にのみ意味を持つ。
        サムネイル保存（max_size=None、store_thumbnail() 経由）では参照されず、
        代わりに短辺 THUMBNAIL_MAX_SHORT_SIDE への縮小（拡大はしない）だけを行う。

        デコード〜エンコードの区間はプロセス全体で1本のロックに通す
        （_DECODE_LOCK のコメント参照。原本を扱う取得元の同時デコードによる
        ピークメモリを避けるため）。

        `self.originals`（`delivers_originals = True` の取得元）のときだけ、
        EXIF Orientation に応じた `exif_transpose` と画素上限チェックを行う
        （**Immich 経路には一切掛けない**。Immich の preview は既に正しい向きで
        返るため、二重回転の恐れがある）。
        """
        global _WEBP_DIRECT_AVAILABLE
        source = raw if isinstance(raw, Path) else BytesIO(raw)
        try:
            with _DECODE_LOCK, ExitStack() as stack:
                # 元画像は ExitStack に載せる。WebP の直接デコードでは、形式・上限・
                # Orientation を確定させたあとに元画像を閉じてから（stack.close()）
                # デコードする（WebPAnimDecoder がファイル全体を常駐させるため）。
                img = stack.enter_context(Image.open(source))
                orientation = None
                direct = False
                if self.originals:
                    # 画素上限（形式別。original_pixel_limit）はデコード前に img.size だけで
                    # 判定できる（Image.open() は遅延読み込みでヘッダしか読まない）。
                    # HEIC の原本を無条件にデコードすると 48MP で maxrss 約602MB
                    # （PoC 実測）になるため、超過分はデコードせずスキップする。
                    px_w, px_h = img.size
                    px_limit = original_pixel_limit(img.format)
                    direct = (_WEBP_DIRECT_AVAILABLE and img.format == 'WEBP'
                              and not getattr(img, 'is_animated', False))
                    if (img.format == 'WEBP' and not direct
                            and px_limit > MAX_ORIGINAL_PIXELS_WEBP_FALLBACK):
                        # アニメーション WebP（WebPDecode は先頭フレームしか扱えない）は
                        # 通常経路（約15MB/MP）でデコードするため、8MP では +137MB に
                        # なる。通常経路に落ちると決まった時点で厳しい上限へ下げる。
                        px_limit = MAX_ORIGINAL_PIXELS_WEBP_FALLBACK
                    if px_w * px_h > px_limit:
                        logger.warning(
                            '原本の画素数が上限を超えているためスキップします: %s %dx%d (上限 %dMP)',
                            img.format, px_w, px_h, px_limit // 1_000_000)
                        return None
                    try:
                        orientation = img.getexif().get(0x0112)
                    except (AttributeError, KeyError, TypeError, ValueError, OSError) as e:
                        logger.debug('EXIF Orientation を読めませんでした: %s', e)
                        orientation = None

                if max_size and img.format == 'JPEG':
                    # draft() は JPEG のみ有効。1/2・1/4 スケールで直接デコードして
                    # デコード負荷とピークメモリを削る。他形式では何も起きない。
                    # cover が必要とする切り抜き元（幅 ≥ max_size 幅 かつ
                    # 高さ ≥ max_size 高さ）は draft() の「要求サイズ以上に
                    # デコードする」という保証でそのまま満たされるため、
                    # cover 用に別の縮小率へ変える必要はない。
                    #
                    # `self.originals` のときは事情が違う: img.size は EXIF
                    # Orientation を適用する前の（回転前の）軸のままなので、
                    # 縦横比が90°効いている写真（Orientation 5-8）へそのまま
                    # max_size を渡すと縦横を取り違えて要求してしまい、
                    # 「縦長の写真が縮小されない」（PoC で確認した不具合）が起きる。
                    # 見た目の向き（effective size）で fit を判定し、その目標寸法を
                    # 回転前の軸へ戻してから draft() へ渡す。
                    if self.originals and orientation in (5, 6, 7, 8):
                        raw_w, raw_h = img.size
                        eff_w, eff_h = raw_h, raw_w
                        pre_fit = resolve_fit(fit, (eff_w, eff_h), max_size)
                        if pre_fit == FIT_COVER:
                            scale = max(max_size[0] / eff_w, max_size[1] / eff_h)
                            eff_target = (max(1, math.ceil(eff_w * scale)),
                                          max(1, math.ceil(eff_h * scale)))
                        else:
                            eff_target = max_size
                        # 軸を回転前へ戻す（幅高さを入れ替える）
                        img.draft('RGB', (eff_target[1], eff_target[0]))
                    else:
                        img.draft('RGB', max_size)
                elif self.originals and img.format == 'JPEG':
                    # 表紙（max_size=None）の原本。draft() を掛けないと、表紙のために
                    # 全画素をデコードしてしまう（階層1の実測: 30MP の JPEG で +123MB）。
                    # 後段が短辺を THUMBNAIL_MAX_SHORT_SIDE へ縮めるので、短辺がそれを
                    # 下回らない縮小率で足りる。正方形を要求すれば「両辺とも
                    # THUMBNAIL_MAX_SHORT_SIDE 以上」が保証され、Orientation 5〜8 の
                    # 縦横入れ替えでも要求が変わらないため軸の取り違えが起きない。
                    # Immich（originals=False）は従来どおり掛けない（出力をバイト単位で
                    # 変えないため）。
                    img.draft('RGB', (THUMBNAIL_MAX_SHORT_SIDE, THUMBNAIL_MAX_SHORT_SIDE))

                direct_img = None
                if direct:
                    # 形式・画素上限・Orientation は確定済み。元画像を閉じて解放してから
                    # デコードする（ピークメモリの削減。_decode_webp_direct のコメント参照）。
                    # 元画像は参照を切らないと WebPAnimDecoder が残るため None にする。
                    stack.close()
                    img = None
                    state, decoded = _decode_webp_direct(raw)
                    if state == _WEBP_SKIP:
                        return None
                    if state == _WEBP_UNAVAILABLE:
                        # 以後は使わない（上限も 4MP へ下がる）。通常経路でやり直す
                        # （_DECODE_LOCK は RLock のため再入できる）。
                        _WEBP_DIRECT_AVAILABLE = False
                        return self._encode_jpeg(raw, max_size, fit)
                    direct_img = self._shrink_then_rotate(decoded, orientation, max_size, fit)
                    del decoded
                if direct_img is not None:
                    img = direct_img
                elif self.originals and orientation in (2, 3, 4, 5, 6, 7, 8):
                    # Orientation が無い（1 や None）ときは呼ばない。回転が無くても
                    # 複製が発生しピークメモリが約20MB増えることを PoC で確認したため
                    # （.claude/context/known-issues.md）。これで以降 img.size は
                    # 見た目どおりの向きになり、直後の resolve_fit() 以降は
                    # Immich 経路と全く同じロジックで扱える。
                    img = ImageOps.exif_transpose(img)

                if direct_img is None:
                    img = self._shrink(img, max_size, fit)

                # RGBA / P モードのままでは JPEG で保存できない
                rgb = img if img.mode == 'RGB' else img.convert('RGB')
                buf = BytesIO()
                try:
                    # 原本モードの写真本体（max_size 指定あり）だけ品質設定を上げる。
                    # 表紙（max_size=None）は原本モードでも現行の設定のまま
                    # （Immich 側の出力はここでバイト単位を変えない）。
                    if self.originals and max_size:
                        rgb.save(buf, format='JPEG', quality=ORIGINALS_JPEG_QUALITY,
                                  subsampling=ORIGINALS_JPEG_SUBSAMPLING)
                    else:
                        rgb.save(buf, format='JPEG', quality=JPEG_QUALITY)
                finally:
                    if rgb is not img:
                        rgb.close()
                return buf.getvalue()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
            # 壊れたデータ・未対応形式・巨大画像（画素上限超過）は握ってスキップする
            # （1枚のために停止させない。DecompressionBombError は Exception 直系で
            # OSError ではないため、別途 except に含める必要がある）
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
