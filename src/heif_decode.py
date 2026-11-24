"""
HEIC/HEIF のタイル単位デコード（libheif を ctypes で直接叩く）

**なぜ自前か。** HEIC の原本を Pillow（pillow-heif）や libheif の全体デコードで開くと、
48MP（iPhone）で maxrss が約412〜602MB になり、RAM 416MB の実機では成り立たない
（`.claude/architecture.md`「画素上限なしで原本をデコードしない」）。iPhone の HEIC は
grid 画像（896x1024 などのタイルを並べたもの）なので、タイルを1枚ずつデコードして
**その場で縮小**し、縮小後の小さなキャンバスへ貼れば、常駐は「タイル1枚＋キャンバス」で
済む（階層1の実測: 48MP で maxrss 約47MB）。この用途は pillow-heif の API では
できない（全体デコードしか無い）。また pillow-heif の最新版は `Pillow>=11.1` を要求し、
`Pillow==10.4.0` 固定（WebP 直接デコードが非公開 API に依存）と両立しないため使わない。

**依存は apt の `libheif1`（`libheif.so.1`）と HEVC プラグイン `libheif-plugin-libde265`
だけ。** Python 側の追加依存は無い。Immich 構成では import されても libheif を読み込まない
（ロードは最初に HEIC を開くときまで遅延する）。

**向きは libheif に任せず、キャンバスへ自前で補正する。** `heif_image_handle_get_image_tiling()`
に `process_image_transformations=1`（回転を考慮したタイル座標）を渡すと、iPhone の
サンプル（grid 9x6、irot=270、8064x6048）で返るオフセットが実際に正しく並ぶ位置と
一致しなかった（原因不明）。そこで **変換を適用しない元の向き**（`process_image_transformations=0`。
オフセットは常に0）でタイル座標を取り、`heif_decoding_options` の
`ignore_transformations=1` でデコードして縮小後のキャンバスに貼り、最後に
`irot` / `imir` を Pillow の transpose で小さなキャンバスへ掛ける
（変換の列は `heif_item_get_transformation_properties()` が item のプロパティ順で返す）。
`clap`（切り抜き）は適用しない（iPhone を含む一般的な HEIC は grid 自体が出力寸法を
持ち clap を使わない。付いていても端が数画素余分に残るだけ）。

構造体の根拠は libheif v1.23.6 の公式ヘッダ（`libheif/api/libheif/heif_tiling.h` /
`heif_decoding.h` / `heif_properties.h` / `heif_metadata.h` / `heif_image.h`）。
`heif_decoding_options` は成長する構造体で version 以降のフィールドが増えるため、
**必ず `heif_decoding_options_alloc()` で確保し、先頭の2バイト（`uint8 version` /
`uint8 ignore_transformations`。version 1 から不変）だけを書き換えて `_free()` で解放する**。
`heif_image_tiling` は version 1 の固定長フィールドだけを使う（`top_offset` /
`left_offset` の順であることに注意。変換なしでは常に 0）。
"""

import ctypes as C
import logging
import math
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

# タイル1枚（grid でない画像は画像全体）の画素数の上限。これを超えるものは全体デコードの
# ピークが大きいので、埋め込みサムネイル（thmb）へ倒す。**仮置きの値**（階層1の
# 実測では 12MP の全体デコードで +100MB 前後）。実機（階層3）で測って決めること。
MAX_TILE_PIXELS = 12_000_000
# 原本全体の画素数の上限。タイルを順に処理するのでメモリは画素数に比例しないが、
# 時間は比例する（階層1の実測: 48MP で 3.6〜5.3 秒。Zero 2 W ではさらに数倍かかる）。
# **仮置きの値**。実機で測って決めること。
MAX_TOTAL_PIXELS = 100_000_000
# 縮小後のキャンバスの画素数の上限。通常は表示寸法の1〜2倍（1024x600 なら 1MP 前後）に
# なるが、タイルの縦横の公約数が小さい特殊な HEIC では縮小率を大きく取れずキャンバスが
# 原寸に近づく。そのときは thmb へ倒してメモリを守る。
MAX_CANVAS_PIXELS = 16_000_000

# タイルの並行デコードのスレッド数。**仮置き。実機（Zero 2 W は4コア）で 1〜4 を測って
# 決めること。** 1 ならスレッドを作らず従来の逐次経路と同じに動く。`decode()` は毎回この
# モジュール定数を参照する（テスト・ベンチはモンキーパッチで差し替える）。
# 実機の1スレッドは HEVC デコードが1コアで律速し 48MP で約26〜30秒かかった。libheif への
# ctypes 呼び出し（`C.CDLL`）は呼び出し中 GIL を解放するので、Python スレッドでも
# 複数コアを使える。一方でスレッドを増やすと CPU を使い切って描画 fps を落としうる
# （`.claude/architecture.md` 参照）。
DECODE_THREADS = 2

# HEVC 系の brand（ftyp の major / compatible のどれかにあれば HEIC とみなす）。
# AVIF（brand が avif / avis）は AV1 デコーダ（dav1d プラグイン）を入れないので対象外。
_HEIC_BRANDS = frozenset((b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis',
                          b'hevm', b'hevs'))
_AVIF_BRANDS = frozenset((b'avif', b'avis'))

# libheif の列挙値（heif_image.h）
_COLORSPACE_RGB = 1
_CHROMA_INTERLEAVED_RGB = 10
_CHANNEL_INTERLEAVED = 10
# heif_item_property_type（heif_properties.h。fourcc を big-endian の整数にしたもの）
_PROP_IROT = int.from_bytes(b'irot', 'big')
_PROP_IMIR = int.from_bytes(b'imir', 'big')
_MIRROR_VERTICAL = 0   # 上下反転
_MIRROR_HORIZONTAL = 1  # 左右反転


class _HeifError(C.Structure):
    """ heif_error（値渡しで返る） """
    _fields_ = [('code', C.c_int), ('subcode', C.c_int), ('message', C.c_char_p)]


class _Tiling(C.Structure):
    """ heif_image_tiling（heif_tiling.h の version 1 までのフィールド） """
    _fields_ = [('version', C.c_int),
                ('num_columns', C.c_uint32), ('num_rows', C.c_uint32),
                ('tile_width', C.c_uint32), ('tile_height', C.c_uint32),
                ('image_width', C.c_uint32), ('image_height', C.c_uint32),
                ('top_offset', C.c_uint32), ('left_offset', C.c_uint32),
                ('number_of_extra_dimensions', C.c_uint8),
                ('extra_dimension_size', C.c_uint32 * 8)]


class _DecodingOptionsHead(C.Structure):
    """ heif_decoding_options の先頭（version 1 から不変の2フィールドだけ） """
    _fields_ = [('version', C.c_uint8), ('ignore_transformations', C.c_uint8)]


_lib: Any = None
_lib_lock = threading.Lock()
_lib_state = 'unloaded'  # 'unloaded' / 'ok' / 'unavailable'

# (関数名, 戻り値, 引数)。1つでも欠けたら「使えない」と見なす
_VP = C.c_void_p
_SIGNATURES: list[tuple[str, Any, list[Any]]] = [
    ('heif_init', _HeifError, [_VP]),
    ('heif_context_alloc', _VP, []),
    ('heif_context_free', None, [_VP]),
    ('heif_context_read_from_file', _HeifError, [_VP, C.c_char_p, _VP]),
    ('heif_context_read_from_memory_without_copy',
     _HeifError, [_VP, C.c_char_p, C.c_size_t, _VP]),
    ('heif_context_get_primary_image_handle', _HeifError, [_VP, C.POINTER(_VP)]),
    ('heif_image_handle_release', None, [_VP]),
    ('heif_image_handle_get_item_id', C.c_uint32, [_VP]),
    ('heif_image_handle_get_width', C.c_int, [_VP]),
    ('heif_image_handle_get_height', C.c_int, [_VP]),
    ('heif_image_handle_get_image_tiling',
     _HeifError, [_VP, C.c_int, C.POINTER(_Tiling)]),
    ('heif_image_handle_decode_image_tile',
     _HeifError, [_VP, C.POINTER(_VP), C.c_int, C.c_int, _VP, C.c_uint32, C.c_uint32]),
    ('heif_decode_image',
     _HeifError, [_VP, C.POINTER(_VP), C.c_int, C.c_int, _VP]),
    ('heif_decoding_options_alloc', _VP, []),
    ('heif_decoding_options_free', None, [_VP]),
    ('heif_image_release', None, [_VP]),
    ('heif_image_get_width', C.c_int, [_VP, C.c_int]),
    ('heif_image_get_height', C.c_int, [_VP, C.c_int]),
    ('heif_image_get_plane_readonly2',
     _VP, [_VP, C.c_int, C.POINTER(C.c_size_t)]),
    ('heif_item_get_transformation_properties',
     C.c_int, [_VP, C.c_uint32, C.POINTER(C.c_uint32), C.c_int]),
    ('heif_item_get_property_type', C.c_int, [_VP, C.c_uint32, C.c_uint32]),
    ('heif_item_get_property_transform_rotation_ccw',
     C.c_int, [_VP, C.c_uint32, C.c_uint32]),
    ('heif_item_get_property_transform_mirror',
     C.c_int, [_VP, C.c_uint32, C.c_uint32]),
    ('heif_image_handle_get_number_of_metadata_blocks', C.c_int, [_VP, C.c_char_p]),
    ('heif_image_handle_get_list_of_metadata_block_IDs',
     C.c_int, [_VP, C.c_char_p, C.POINTER(C.c_uint32), C.c_int]),
    ('heif_image_handle_get_metadata_size', C.c_size_t, [_VP, C.c_uint32]),
    ('heif_image_handle_get_metadata', _HeifError, [_VP, C.c_uint32, _VP]),
    ('heif_image_handle_get_number_of_thumbnails', C.c_int, [_VP]),
    ('heif_image_handle_get_list_of_thumbnail_IDs',
     C.c_int, [_VP, C.POINTER(C.c_uint32), C.c_int]),
    ('heif_image_handle_get_thumbnail',
     _HeifError, [_VP, C.c_uint32, C.POINTER(_VP)]),
]


def _load() -> Any:
    """
    libheif を1回だけロードして返す。使えなければ None（警告は1回だけ）。
    WebP の直接デコード（`_WEBP_DIRECT_AVAILABLE`）と同じ流儀で、ライブラリ・
    タイル API・必要な関数のどれかが無いときは HEIC を「対応しない」へ倒す。
    """
    global _lib, _lib_state
    if _lib_state != 'unloaded':
        return _lib
    with _lib_lock:
        if _lib_state != 'unloaded':
            return _lib
        try:
            lib = C.CDLL('libheif.so.1')
            for name, restype, argtypes in _SIGNATURES:
                fn = getattr(lib, name)  # 無ければ AttributeError
                fn.restype = restype
                fn.argtypes = argtypes
            err = lib.heif_init(None)
            if err.code != 0:
                raise OSError(f'heif_init が失敗しました: {err.message!r}')
        except (OSError, AttributeError) as e:
            logger.warning('libheif を使えないため HEIC/HEIF をスキップします: %s', e)
            _lib, _lib_state = None, 'unavailable'
        else:
            _lib, _lib_state = lib, 'ok'
            logger.info('libheif を読み込みました（HEIC/HEIF のタイル単位デコードが有効）')
        return _lib


def available() -> bool:
    """ libheif が使えるか（初回呼び出しでロードする。HEIC を扱わない構成では呼ばれない） """
    return _load() is not None


def is_heif(head: bytes) -> bool:
    """
    ファイル先頭のバイト列（64バイトほどで足りる）から HEIC/HEIF かを brand で判定する。
    拡張子には依存しない（`.heic` を名乗る別形式・拡張子の無い S3 キーに備えるため）。
    ftyp の major brand と compatible brands のどれかが HEVC 系なら True。
    AVIF は False。major が `mif1` / `msf1` だけの汎用 HEIF も、AVIF の印が無ければ True。
    """
    if len(head) < 16 or head[4:8] != b'ftyp':
        return False
    size = int.from_bytes(head[0:4], 'big')
    box = head[8:size] if 16 <= size <= len(head) else head[8:]
    brands = {box[0:4]}
    brands.update(box[i:i + 4] for i in range(8, len(box) - 3, 4))
    if brands & _AVIF_BRANDS:
        return False
    return bool(brands & _HEIC_BRANDS) or box[0:4] in (b'mif1', b'msf1')


def sniff(source: 'bytes | Path') -> bool:
    """ `bytes` または `Path` の先頭を読んで `is_heif()` で判定する（読めなければ False） """
    try:
        if isinstance(source, Path):
            with open(source, 'rb') as f:
                head = f.read(64)
        else:
            head = bytes(source[:64])
    except OSError:
        return False
    return is_heif(head)


def _transpose_for(kind: str, value: int) -> Any:
    """ 変換1つぶんの Pillow の Transpose を返す（libheif の値 -> Pillow） """
    if kind == 'irot':
        # libheif は反時計回りの角度（0/90/180/270）。Pillow の ROTATE_90 も反時計回り
        return {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
                270: Image.Transpose.ROTATE_270}.get(value)
    return (Image.Transpose.FLIP_TOP_BOTTOM if value == _MIRROR_VERTICAL
            else Image.Transpose.FLIP_LEFT_RIGHT)


class HeifImage:
    """
    開いた HEIC/HEIF 1ファイル（primary image）。`open_heif()` で作り、`with` か
    `close()` で解放する。**スレッド間で共有しない**（`PhotoCache` は `_DECODE_LOCK` の中で
    使う）。メタデータ（`size` / `exif()`）は画素をデコードしないので軽い。
    """

    def __init__(self, lib: Any, ctx: Any, handle: Any, keep: Any,
                 source: 'bytes | Path | None' = None) -> None:
        self._lib = lib
        self._source = source  # 並行デコードでスレッドごとに開き直すための入力
        self._ctx = ctx
        self._handle = handle
        self._keep = keep  # read_from_memory_without_copy で渡したメモリの生存を保つ
        self._item_id = lib.heif_image_handle_get_item_id(handle)

        tiling = _Tiling()
        tiling.version = 1
        err = lib.heif_image_handle_get_image_tiling(handle, 0, C.byref(tiling))
        if err.code != 0:
            raise OSError(f'タイル情報を取得できません: {err.message!r}')
        self._tiling = tiling
        self.raw_size = (int(tiling.image_width), int(tiling.image_height))
        self.tile_size = (int(tiling.tile_width), int(tiling.tile_height))
        self.is_tiled = tiling.num_columns * tiling.num_rows > 1
        self._transforms = self._read_transforms()
        quarter = any(k == 'irot' and v in (90, 270) for k, v in self._transforms)
        w, h = self.raw_size
        # 見た目の寸法（回転後）。clap は適用しないので影響しない
        self.size = (h, w) if quarter else (w, h)

    def __enter__(self) -> 'HeifImage':
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """ ハンドルとコンテキストを解放する（二重呼び出し可） """
        if self._handle:
            self._lib.heif_image_handle_release(self._handle)
            self._handle = None
        if self._ctx:
            self._lib.heif_context_free(self._ctx)
            self._ctx = None
        self._keep = None

    # ------------------------------------------------------------ メタデータ

    def _read_transforms(self) -> list[tuple[str, int]]:
        """ irot / imir を item のプロパティ順に [('irot', 角度) / ('imir', 向き)] で返す """
        lib, ctx, item = self._lib, self._ctx, self._item_id
        count = lib.heif_item_get_transformation_properties(ctx, item, None, 0)
        if count <= 0:
            return []
        ids = (C.c_uint32 * count)()
        count = lib.heif_item_get_transformation_properties(ctx, item, ids, count)
        out: list[tuple[str, int]] = []
        for i in range(max(count, 0)):
            ptype = lib.heif_item_get_property_type(ctx, item, ids[i])
            if ptype == _PROP_IROT:
                out.append(('irot', lib.heif_item_get_property_transform_rotation_ccw(
                    ctx, item, ids[i])))
            elif ptype == _PROP_IMIR:
                out.append(('imir', lib.heif_item_get_property_transform_mirror(
                    ctx, item, ids[i])))
        return out

    def exif(self) -> bytes | None:
        """
        Exif を TIFF ヘッダから始まるバイト列で返す（`provider_util.parse_exif_date()` に
        そのまま渡せる）。無ければ None。HEIF の Exif ブロックは先頭4バイトが
        「TIFF ヘッダまでのオフセット」（big-endian）なので、それを読んで取り除く。
        Exif 内の Orientation は見ない（HEIF では `irot` / `imir` が正）。
        """
        lib, handle = self._lib, self._handle
        # list_of_...IDs は「count 個まで書き込む」だけで、NULL による個数問い合わせは
        # できない（0 が返る）ため、個数は専用の関数で先に取る
        n = lib.heif_image_handle_get_number_of_metadata_blocks(handle, b'Exif')
        if n <= 0:
            return None
        ids = (C.c_uint32 * n)()
        n = lib.heif_image_handle_get_list_of_metadata_block_IDs(handle, b'Exif', ids, n)
        if n <= 0:
            return None
        size = lib.heif_image_handle_get_metadata_size(handle, ids[0])
        # 異常に大きい Exif（壊れたファイル）でメモリを使わない
        if size < 8 or size > 1_000_000:
            return None
        buf = C.create_string_buffer(size)
        err = lib.heif_image_handle_get_metadata(handle, ids[0], buf)
        if err.code != 0:
            return None
        data = buf.raw
        offset = int.from_bytes(data[:4], 'big')
        start = 4 + offset
        return data[start:] if start < len(data) else None

    # ------------------------------------------------------------ デコード

    def _apply_transforms(self, img: Image.Image) -> Image.Image:
        """ irot / imir を、縮小済みの小さな画像へ順に掛ける """
        for kind, value in self._transforms:
            method = _transpose_for(kind, value)
            if method is not None:
                img = img.transpose(method)
        return img

    def _decode_to_image(self, handle: Any, tile: tuple[int, int] | None) -> Image.Image:
        """
        1枚（grid のタイル、または画像全体）を RGB 8bit interleaved でデコードし、
        PIL Image（libheif のバッファからコピー済み）にして返す。10bit 等の画像は
        libheif が 8bit RGB へ変換する（要求した chroma へ色変換を行う）。
        変換（回転等）は無視する（`ignore_transformations=1`）。呼び出し側が向きを補正する。
        """
        lib = self._lib
        opts = lib.heif_decoding_options_alloc()
        if not opts:
            raise MemoryError('heif_decoding_options を確保できません')
        try:
            C.cast(opts, C.POINTER(_DecodingOptionsHead)).contents.ignore_transformations = 1
            out = C.c_void_p()
            if tile is None:
                err = lib.heif_decode_image(handle, C.byref(out), _COLORSPACE_RGB,
                                            _CHROMA_INTERLEAVED_RGB, opts)
            else:
                err = lib.heif_image_handle_decode_image_tile(
                    handle, C.byref(out), _COLORSPACE_RGB, _CHROMA_INTERLEAVED_RGB, opts,
                    tile[0], tile[1])
        finally:
            lib.heif_decoding_options_free(opts)
        if err.code != 0:
            raise OSError(f'デコードに失敗しました: {err.message!r}')
        try:
            w = lib.heif_image_get_width(out, _CHANNEL_INTERLEAVED)
            h = lib.heif_image_get_height(out, _CHANNEL_INTERLEAVED)
            stride = C.c_size_t()
            ptr = lib.heif_image_get_plane_readonly2(out, _CHANNEL_INTERLEAVED,
                                                     C.byref(stride))
            if not ptr or w <= 0 or h <= 0 or stride.value < w * 3:
                raise OSError('デコード結果のプレーンが不正です')
            buf = (C.c_char * (stride.value * h)).from_address(ptr)
            # frombuffer は libheif のバッファを参照するだけなので、解放前に copy() する
            return Image.frombuffer('RGB', (w, h), buf, 'raw', 'RGB', stride.value, 1).copy()
        finally:
            lib.heif_image_release(out)

    def decode(self, min_scale: float) -> Image.Image | None:
        """
        見た目の向きに補正した RGB 画像を返す。縮小率は `min_scale`（原寸に対する倍率）
        以上で、整数分の1（`1/N`）に丸まる。1 以上は原寸。最終の縮小・fit は呼び出し側
        （`PhotoCache._shrink()`）が行うので、ここで返す寸法は必要寸法の1〜2倍程度になる。
        失敗（デコーダ無し・壊れたタイル・上限超過で thmb も無い）は None。

        - **埋め込みサムネイル（thmb）が必要寸法を満たすなら、それだけをデコードして返す**
          （表紙の短辺 250px など。48MP のタイルを全部デコードするより桁違いに速い）。
          表紙専用の分岐ではなく、写真本体にも同じ判定を掛ける。
        - 通常はタイルを1枚ずつデコード -> 整数倍で縮小（`Image.reduce`。BOX 平均）->
          キャンバスへ貼る（常駐はタイル1枚＋キャンバス）。grid でない画像はタイル1枚
          （＝全体）として同じ経路を通る。`DECODE_THREADS` が 2 以上なら、タイル1枚ぶんの
          処理（デコード・crop・reduce）を並行に行う（`_decode_tiles_parallel`）。
        - **縮小は整数倍にする。** タイルごとに任意の倍率で LANCZOS をかけると、タイルの
          寸法が倍率で割り切れず境界の画素の位置がタイルごとにずれる（階層1の実測:
          倍率 0.2 で全体を一括で縮めた結果と平均 2.3/255 食い違い、タイル内で
          境界から離れるほど増えた）。N がタイルの縦横の公約数なら境界がちょうど
          画素の境目に合い、一括で縮めた結果と一致する（0.25 / 0.5 で平均 0.2 以下）。
          そのため N は「タイルの縦横の最大公約数の約数」のうち 1/min_scale 以下で最大のもの。
        - タイル1枚の画素数が `MAX_TILE_PIXELS` を超える、全体が `MAX_TOTAL_PIXELS` を
          超える、またはキャンバスが `MAX_CANVAS_PIXELS` を超える（タイルの寸法が
          素数に近く N を大きく取れない）ときは、埋め込みサムネイル（thmb）を使う。
          無ければ None（スキップ）。
        """
        try:
            w, h = self.raw_size
            tw, th = self.tile_size
            # 必要寸法（見た目の向き）は min_scale × 原寸。式は下の縮小率 N の決定と同じ min_scale
            thumb = self._best_thumbnail()
            if thumb is not None and min_scale < 1.0:
                vis_w, vis_h = self.size
                if thumb[1] >= min_scale * vis_w - 1e-6 and thumb[2] >= min_scale * vis_h - 1e-6:
                    logger.info('HEIC の埋め込みサムネイル（%dx%d）が必要寸法を満たすため、'
                                'タイルをデコードせずサムネイルを使います', thumb[1], thumb[2])
                    return self._decode_thumbnail(thumb[0])
            if w * h > MAX_TOTAL_PIXELS:
                logger.warning('HEIC の画素数が上限を超えているためスキップします: %dx%d '
                               '(上限 %dMP)', w, h, MAX_TOTAL_PIXELS // 1_000_000)
                return None
            if tw * th > MAX_TILE_PIXELS:
                return self._decode_thumbnail_fallback()
            want = max(1, int(1.0 / max(min(min_scale, 1.0), 1e-6) + 1e-9))
            if self.is_tiled:
                g = math.gcd(tw, th)
                n = max(d for d in range(1, min(want, g) + 1) if g % d == 0)
            else:
                n = want  # 画像全体を1回でデコードするので境界の問題が無い
            cw, ch = -(-w // n), -(-h // n)
            if cw * ch > MAX_CANVAS_PIXELS:
                logger.info('HEIC のタイル寸法が縮小に向かないため埋め込みサムネイルを使います: '
                            'tile=%dx%d', tw, th)
                return self._decode_thumbnail_fallback()
            canvas = Image.new('RGB', (cw, ch))
            cols, rows = self._tiling.num_columns, self._tiling.num_rows
            coords = [(tx, ty) for ty in range(rows) for tx in range(cols)]
            threads = min(int(DECODE_THREADS), len(coords))
            if threads <= 1 or not self.is_tiled:
                for tx, ty in coords:
                    self._paste(canvas, self._decode_tile(self._handle, tx, ty, n))
            else:
                self._decode_tiles_parallel(canvas, coords, n, threads)
            return self._apply_transforms(canvas)
        except (OSError, MemoryError, ValueError) as e:
            logger.warning('HEIC をデコードできませんでした: %s', e)
            return None

    def _decode_tile(self, handle: Any, tx: int, ty: int,
                     n: int) -> tuple[int, int, Image.Image] | None:
        """
        タイル1枚ぶんの処理（デコード -> 画像外へはみ出す分を crop -> `reduce(n)`）。
        貼る位置（縮小後のキャンバス座標）と縮小済みタイルを返す。画像外なら None。
        ワーカースレッドから呼ばれるので、キャンバスには触らない（貼るのは呼び出し元だけ）。
        """
        w, h = self.raw_size
        tw, th = self.tile_size
        tile = self._decode_to_image(handle, (tx, ty) if self.is_tiled else None)
        # 画像右端・下端のタイルは画像外へはみ出す。貼る範囲は画像内に収める
        x0, y0 = tx * tw, ty * th
        vw, vh = min(tile.width, w - x0), min(tile.height, h - y0)
        if vw <= 0 or vh <= 0:
            return None
        if (vw, vh) != tile.size:
            tile = tile.crop((0, 0, vw, vh))
        if n > 1:
            tile = tile.reduce(n)
        return x0 // n, y0 // n, tile

    @staticmethod
    def _paste(canvas: Image.Image, done: tuple[int, int, Image.Image] | None) -> None:
        """ `_decode_tile()` の結果をキャンバスへ貼る（呼び出し元スレッドだけが呼ぶ） """
        if done is not None:
            canvas.paste(done[2], (done[0], done[1]))

    def _decode_tiles_parallel(self, canvas: Image.Image, coords: list[tuple[int, int]],
                               n: int, threads: int) -> None:
        """
        タイルを `threads` 本のワーカーで並行にデコードし、結果は呼び出し元スレッドが貼る。

        - **スレッドごとに libheif の context / handle を開く。** 同一 context への同時デコードの
          スレッド安全性を libheif のドキュメントで確認できていないため、共有しない。
          Path 入力はスレッドごとにファイルを開き、bytes 入力は同じバッファを
          `read_from_memory_without_copy` で共有する（`self._keep` が生存を保つ。読み取り専用）。
        - 同時に処理中（デコード中＋貼り待ち）のタイルを「スレッド数+1」に制限する。
          全タイルを投入すると完成済みタイルが溜まってメモリが膨らむため。
        - 1枚でも失敗したら例外を送出する（呼び出し元が None にする）。`finally` で必ず
          全ワーカーの終了を待ってから context を解放する（リーク・解放済み handle の
          使用を避ける）。
        """
        local = threading.local()
        opened: list[tuple[Any, Any]] = []
        opened_lock = threading.Lock()

        def thread_handle() -> Any:
            handle = getattr(local, 'handle', None)
            if handle is None:
                ctx, handle = _open_handle(self._lib, self._source, self._keep)
                with opened_lock:
                    opened.append((ctx, handle))
                local.handle = handle
            return handle

        def job(tx: int, ty: int) -> tuple[int, int, Image.Image] | None:
            return self._decode_tile(thread_handle(), tx, ty, n)

        pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix='heif-tile')
        pending: set[Future] = set()
        try:
            for tx, ty in coords:
                while len(pending) >= threads + 1:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for f in done:
                        self._paste(canvas, f.result())
                pending.add(pool.submit(job, tx, ty))
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for f in done:
                    self._paste(canvas, f.result())
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            for ctx, handle in opened:
                self._lib.heif_image_handle_release(handle)
                self._lib.heif_context_free(ctx)

    def _best_thumbnail(self) -> tuple[int, int, int] | None:
        """
        埋め込みサムネイルのうち最大のもの `(thumbnail ID, 幅, 高さ)` を返す（無ければ None）。
        寸法は画素をデコードせず handle から取る（見た目の向きの値。サンプルで確認済み）。
        """
        lib = self._lib
        n = lib.heif_image_handle_get_number_of_thumbnails(self._handle)
        if n <= 0:
            return None
        ids = (C.c_uint32 * n)()
        n = lib.heif_image_handle_get_list_of_thumbnail_IDs(self._handle, ids, n)
        best: tuple[int, int, int] | None = None
        for i in range(max(n, 0)):
            thumb = C.c_void_p()
            err = lib.heif_image_handle_get_thumbnail(self._handle, ids[i], C.byref(thumb))
            if err.code != 0:
                continue
            try:
                tw = lib.heif_image_handle_get_width(thumb)
                th = lib.heif_image_handle_get_height(thumb)
            finally:
                lib.heif_image_handle_release(thumb)
            if tw > 0 and th > 0 and (best is None or tw * th > best[1] * best[2]):
                best = (int(ids[i]), int(tw), int(th))
        return best

    def _decode_thumbnail_fallback(self) -> Image.Image | None:
        """ タイル経路を諦めて thmb へ倒す（上限超過時。サイズ判定で選ぶ経路とは別） """
        thumb = self._best_thumbnail()
        if thumb is None:
            logger.warning('HEIC のタイルが大きすぎ、サムネイルも無いためスキップします')
            return None
        logger.info('HEIC のタイルが大きいため埋め込みサムネイルを使います')
        return self._decode_thumbnail(thumb[0])

    def _decode_thumbnail(self, thumb_id: int) -> Image.Image | None:
        """ 埋め込みサムネイル（thmb）をデコードする。取得できなければ None """
        lib = self._lib
        thumb = C.c_void_p()
        err = lib.heif_image_handle_get_thumbnail(self._handle, thumb_id, C.byref(thumb))
        if err.code != 0:
            logger.warning('HEIC のサムネイルを取得できません: %r', err.message)
            return None
        try:
            # サムネイルは primary の変換（向き）を引き継ぐ。thmb 自体は変換を持たない
            return self._apply_transforms(self._decode_to_image(thumb, None))
        finally:
            lib.heif_image_handle_release(thumb)


def _open_handle(lib: Any, source: 'bytes | Path | None', keep: Any) -> tuple[Any, Any]:
    """
    context を作って primary image の handle を取る。`(ctx, handle)` を返す。
    失敗したら context を解放して OSError を送出する。`keep` は bytes 入力のバッファ
    （`read_from_memory_without_copy` が参照し続けるので、呼び出し元が生存を保つこと）。
    """
    ctx = lib.heif_context_alloc()
    if not ctx:
        raise OSError('heif_context を確保できません')
    handle = C.c_void_p()
    try:
        if isinstance(source, Path):
            err = lib.heif_context_read_from_file(
                ctx, str(source).encode('utf-8', 'surrogateescape'), None)
        else:
            err = lib.heif_context_read_from_memory_without_copy(ctx, keep, len(keep), None)
        if err.code == 0:
            err = lib.heif_context_get_primary_image_handle(ctx, C.byref(handle))
        if err.code != 0:
            raise OSError(f'HEIC を開けませんでした: {err.message!r}')
    except BaseException:
        lib.heif_context_free(ctx)
        raise
    return ctx, handle


def open_heif(source: 'bytes | Path') -> HeifImage | None:
    """
    HEIC/HEIF を開く。libheif が使えない・壊れている・デコーダ（HEVC プラグイン）が
    無いときは None。画素はデコードしない（メタデータの読み込みだけ）。
    """
    lib = _load()
    if lib is None:
        return None
    keep = None if isinstance(source, Path) else bytes(source)
    try:
        ctx, handle = _open_handle(lib, source, keep)
    except OSError as e:
        logger.warning('%s', e)
        return None
    try:
        return HeifImage(lib, ctx, handle, keep, source if isinstance(source, Path) else keep)
    except (OSError, ValueError) as e:
        logger.warning('HEIC の情報を読めませんでした: %s', e)
        lib.heif_image_handle_release(handle)
        lib.heif_context_free(ctx)
        return None
    except BaseException:
        lib.heif_image_handle_release(handle)
        lib.heif_context_free(ctx)
        raise


def read_exif(path: Path) -> bytes | None:
    """
    ローカル取得元が一覧の段階で撮影日を読むためのもの。画素はデコードしない。
    開けない・Exif が無いときは None。
    """
    img = open_heif(path)
    if img is None:
        return None
    with img:
        return img.exif()
