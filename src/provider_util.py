"""
写真取得元の実装（local_api / s3_api）と PhotoCache が共用する小さな部品

ID の符号化（base64 / ハッシュ）と、EXIF の撮影日の解析。もともと `local_api.py` に
あったものを、S3 取得元でも同じ形式の ID と日付解析を使うために切り出した。
複製すると ID の形式（キャッシュのファイル名になる）や日付の書式がずれるため、
ここ1か所に置く。標準ライブラリと Pillow だけに依存し、他の自作モジュールは
import しない（photo_cache / local_api / s3_api のどれから import しても循環しない）。
"""

import base64
import hashlib
from datetime import datetime

from PIL import Image

EXIF_IFD_POINTER = 0x8769
EXIF_DATETIME_ORIGINAL = 0x9003
# 日付補助ファイル・写真リストの `date` が取る書式（gdrive_api の _normalize_time と同じ）
ISO_DATE_FORMAT = '%Y-%m-%dT%H:%M:%S'


def b64e(text: str) -> str:
    """ 文字列を URL セーフな base64（パディング無し）へ。非 UTF-8 は元のバイト列を保つ """
    raw = text.encode('utf-8', 'surrogateescape')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def b64d(token: str) -> str:
    """ `b64e()` の逆変換。不正な入力は binascii.Error / UnicodeDecodeError / ValueError """
    pad = '=' * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + pad).decode('utf-8', 'surrogateescape')


def display_name(text: str) -> str:
    """
    表示・ログ用の名前へ直す。非 UTF-8 の名前は surrogateescape の孤立サロゲートを
    含み、そのまま写真リスト JSON や settings.json へ書くと UnicodeEncodeError で
    途中までしか書けない。ID（base64）は元のバイト列のままなので、置換文字に
    するのは表示名だけ。
    """
    return text.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')


def sha_prefix(text: str, n: int) -> str:
    """ SHA-1 の先頭 n 文字（ID の短縮・署名用。暗号用途ではない） """
    return hashlib.sha1(text.encode('utf-8', 'surrogateescape')).hexdigest()[:n]


def parse_exif_date(raw: bytes) -> str:
    """
    EXIF のバイト列から DateTimeOriginal を取り出し、ISO（秒まで）で返す。
    日付が無い・書式が合わないときは空文字。バイト列が壊れていると Pillow の
    例外がそのまま上がるので、呼び出し側が握ってログに残す。

    **`img.getexif()` ではなく `info['exif']` のバイト列を渡す前提。** Pillow 10.4 の
    PNG は `info` に 'exif' が無いと `getexif()` が全画素をデコードする
    （階層1の実測: 6000x5000 の PNG で +117MB）ため、撮影日のためにそれをしない。
    """
    exif = Image.Exif()
    exif.load(raw)
    value = exif.get_ifd(EXIF_IFD_POINTER).get(EXIF_DATETIME_ORIGINAL)
    if not value:
        return ''
    if isinstance(value, bytes):
        value = value.decode('ascii', 'ignore')
    try:
        return datetime.strptime(str(value).strip('\x00 '),
                                 '%Y:%m:%d %H:%M:%S').strftime(ISO_DATE_FORMAT)
    except ValueError:
        return ''
