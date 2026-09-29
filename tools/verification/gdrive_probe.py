"""
Google Drive API の疎通確認（PR0 / PoC）

サービスアカウント（SA）でトークンを取得し、共有フォルダ（ルート）直下の
サブフォルダとファイルを列挙する。`.claude/plans/abundant-weaving-kernighan.md`
PR0 の通過条件（SA で共有フォルダが読めること）を確かめるためのワンショット。

**google-api-python-client は使わない。** 方針どおり google-auth でトークンだけ
取得し、Drive REST v3 は既存の requests で直接呼ぶ（gdrive_api.py の実装方針と
揃えるため、ここで REST 呼び出しの形を先に確かめる）。

依存（google-auth）は Dev Container にもコンテナイメージにも入っていない。
`--target` で入れた site ディレクトリを `PYTHONPATH` で渡して実行する前提。

    pip install --only-binary=:all: --target /path/to/site \\
        google-auth==2.58.1 pillow-heif==1.8.0
    PYTHONPATH=/path/to/site python tools/verification/gdrive_probe.py \\
        --key /path/to/sa.json --root <フォルダID>

秘匿情報（SA 鍵の中身・アクセストークンの値）はログに出さない。

Dev Container のシステム時刻は ±27.9秒 ほど往復することがある
（`.claude/context/known-issues.md` 参照）。JWT の署名時刻がサーバー側の許容範囲を
外れると `invalid_grant` になりうるため、トークン取得の前後で time.time() と
datetime.utcnow() の両方をログへ残す。

実行方法（リポジトリルートから）:

    python tools/verification/gdrive_probe.py --key sa.json --root <フォルダID>

実機では切り離して実行し、ログを回収する
（`.claude/workflows.md` の「長い処理は切り離して実行する」を参照）。

    setsid nohup python tools/verification/gdrive_probe.py --key sa.json \\
        --root <フォルダID> > gdrive_probe.log 2>&1 < /dev/null &
    grep -q GDRIVE_PROBE_ALLDONE gdrive_probe.log
"""

import argparse
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('gdrive_probe')

# 完了判定に使うマーカー（.claude/workflows.md「長い処理は切り離して実行する」参照）
DONE_MARKER = 'GDRIVE_PROBE_ALLDONE'

DRIVE_API_BASE = 'https://www.googleapis.com/drive/v3'
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
FOLDER_MIME = 'application/vnd.google-apps.folder'
FILE_FIELDS = ('id,name,mimeType,size,md5Checksum,createdTime,modifiedTime,'
               'imageMediaMetadata(width,height,rotation,time),shortcutDetails')
LIST_FIELDS = f'nextPageToken,files({FILE_FIELDS})'

# fileId として許容される文字集合。'.' を含む fileId が実在するかを確認する対象
# （<fileId>.<md5先頭8桁> をキャッシュファイル名に使う PR3 の設計の前提になる）
SAFE_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')

IMAGE_MIME_PREFIXES = ('image/jpeg', 'image/heic', 'image/heif')
IMAGE_MIME_PREFIX_ANY = ('image/',)

# thumbnailLink の確認に使う既定のサイズ指定（=wNNN-hNNN 形式 / =sNNN 形式）
DEFAULT_THUMB_SIZES = 'w1024-h600,s1024'
DEFAULT_THUMB_MAX = 6

THUMB_FIELDS = 'name,mimeType,hasThumbnail,thumbnailLink,imageMediaMetadata'


def _try_import_deps():
    """ google-auth / requests を import する。失敗時は分かりやすいメッセージで終了する """
    try:
        import requests  # noqa: F401
    except ImportError as e:
        logger.error(
            'requests が import できません。Dev Container / イメージには標準で '
            '入っているはずです: %s', e)
        sys.exit(2)

    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.service_account import Credentials  # noqa: F401
    except ImportError as e:
        logger.error(
            'google-auth が import できません。このスクリプトは依存を'
            ' PYTHONPATH 経由で受け取る前提です（Dev Container にもイメージにも'
            '未導入）。tools/verification/gdrive_poc_runner.sh を参照し、'
            'pip install --target <dir> google-auth==2.58.1 の後 '
            'PYTHONPATH=<dir> で実行してください: %s', e)
        sys.exit(2)


def get_access_token(key_path: str):
    """ SA 鍵からアクセストークンを取得する。戻り値は (token, expiry) """
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials

    now_time = time.time()
    now_utc = datetime.now(timezone.utc)
    logger.info('トークン取得前のローカル時刻: time.time()=%.3f utcnow=%s',
                now_time, now_utc.isoformat())

    creds = Credentials.from_service_account_file(key_path, scopes=DRIVE_SCOPES)
    creds.refresh(Request())

    logger.info('トークン取得後のローカル時刻: time.time()=%.3f', time.time())
    logger.info('トークン取得に成功しました: 長さ=%d expiry=%s',
                len(creds.token or ''), creds.expiry)
    return creds.token, creds.expiry


def _get(session, path: str, params: dict, timeout: float = 30.0):
    """ Drive REST を GET する。エラーはステータスと本文の先頭500文字をログに残す """
    resp = session.get(f'{DRIVE_API_BASE}/{path}', params=params, timeout=timeout)
    if not resp.ok:
        logger.error('HTTP %d: %s', resp.status_code, resp.text[:500])
        if resp.status_code in (403, 404):
            logger.error('403/404 は SA へのフォルダ共有漏れの可能性があります')
    return resp


def list_folder(session, folder_id: str) -> list[dict]:
    """ folder_id 直下のファイル・フォルダをページ送りで全件列挙する """
    files: list[dict] = []
    page_token = None
    page_count = 0
    while True:
        params = {
            'q': f"'{folder_id}' in parents and trashed=false",
            'pageSize': 1000,
            'supportsAllDrives': 'true',
            'includeItemsFromAllDrives': 'true',
            'fields': LIST_FIELDS,
        }
        if page_token:
            params['pageToken'] = page_token
        resp = _get(session, 'files', params)
        if not resp.ok:
            break
        page_count += 1
        data = resp.json()
        files.extend(data.get('files', []))
        page_token = data.get('nextPageToken')
        if not page_token:
            break
    logger.info('フォルダ %s: %d 件（%d ページ）', folder_id, len(files), page_count)
    return files


def download_file(session, file_id: str, dest: Path) -> tuple[float, int]:
    """ ファイルを原本のままストリーミング取得して dest へ書き出す。(所要秒, バイト数) を返す """
    start = time.monotonic()
    total = 0
    with session.get(
        f'{DRIVE_API_BASE}/files/{file_id}',
        params={'alt': 'media', 'supportsAllDrives': 'true'},
        timeout=60,
        stream=True,
    ) as resp:
        resp.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                f.write(chunk)
                total += len(chunk)
    elapsed = time.monotonic() - start
    return elapsed, total


def inspect_downloaded(dest: Path, drive_rotation) -> None:
    """ ダウンロードした画像を Pillow で開き、EXIF Orientation と Drive 側の rotation を並べる """
    try:
        from PIL import Image
    except ImportError as e:
        logger.warning('Pillow が import できず内容を確認できません: %s', e)
        return

    suffix = dest.suffix.lower()
    if suffix in ('.heic', '.heif'):
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError as e:
            logger.warning('pillow_heif が無く HEIC を開けません: %s', e)
            return

    try:
        with Image.open(dest) as im:
            orientation = im.getexif().get(0x0112)
            logger.info(
                '  Pillow: format=%s size=%s mode=%s EXIF Orientation=%s '
                '（Drive rotation=%s）',
                im.format, im.size, im.mode, orientation, drive_rotation)
    except Exception as e:
        logger.warning('  Pillow で開けませんでした: %s', e)


def _redact_thumb_url(url: str, size_spec: str) -> str:
    """ ログに出してよい形へ縮める。ホスト名とサイズ指定だけを残し、署名付きの
    パス・クエリ（トークン相当）はログに出さない """
    host = urlsplit(url).netloc
    return f'{host}/...(redacted)=?size={size_spec}'


def get_file_metadata(session, file_id: str) -> dict | None:
    """ files.get で hasThumbnail / thumbnailLink / imageMediaMetadata を取り直す。
    一覧取得（files.list）とは別のフィールドセットで、サムネイル確認専用 """
    resp = _get(session, f'files/{file_id}',
                {'fields': THUMB_FIELDS, 'supportsAllDrives': 'true'})
    if not resp.ok:
        logger.error('files.get 失敗 id=%s: HTTP %d', file_id, resp.status_code)
        return None
    return resp.json()


def select_thumbnail_candidates(all_files: list[dict], max_n: int) -> list[dict]:
    """ サムネイル確認の対象を選ぶ。優先順位は
    (1) imageMediaMetadata.rotation が 0 以外 (2) HEIC/HEIF (3) JPEG (4) その他の画像 """
    def score(f: dict) -> int:
        mime = str(f.get('mimeType', ''))
        meta = f.get('imageMediaMetadata') or {}
        rotation = meta.get('rotation')
        if rotation not in (None, 0):
            return 0
        if mime.startswith(('image/heic', 'image/heif')):
            return 1
        if mime.startswith('image/jpeg'):
            return 2
        return 3

    candidates = [
        f for f in all_files
        if f.get('mimeType') != FOLDER_MIME
        and str(f.get('mimeType', '')).startswith(IMAGE_MIME_PREFIX_ANY)
    ]
    candidates.sort(key=score)
    return candidates[:max_n]


def _open_image_info(path: Path) -> dict | None:
    """ 画像ファイルを Pillow で開き、寸法・EXIF Orientation を返す。
    HEIC/HEIF は pillow_heif が import できるときだけ開く """
    try:
        from PIL import Image
    except ImportError as e:
        logger.warning('Pillow が import できず内容を確認できません: %s', e)
        return None

    suffix = path.suffix.lower()
    if suffix in ('.heic', '.heif'):
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError as e:
            logger.warning('pillow_heif が無く HEIC を開けません: %s', e)
            return None

    try:
        with Image.open(path) as im:
            orientation = im.getexif().get(0x0112)
            return {
                'format': im.format,
                'size': im.size,
                'mode': im.mode,
                'exif_orientation': orientation,
            }
    except Exception as e:
        logger.warning('  Pillow で開けませんでした（%s）: %s', path, e)
        return None


def _find_downloaded_original(download_dir: Path, file_id: str) -> Path | None:
    """ --max-download で既に取得済みの原本ファイルを id から逆引きする """
    matches = sorted(download_dir.glob(f'{file_id}.*'))
    return matches[0] if matches else None


def _effective_landscape(width: int | None, height: int | None,
                          drive_rotation, exif_orientation) -> bool | None:
    """ Drive の rotation・EXIF Orientation を適用した後、横長かどうかを返す。
    正方形または寸法不明のときは None（判定不能） """
    if width is None or height is None:
        return None
    w, h = width, height
    if drive_rotation in (90, 270):
        w, h = h, w
    if exif_orientation in (5, 6, 7, 8):
        w, h = h, w
    if w == h:
        return None
    return w > h


def probe_thumbnails(session, requests_mod, all_files: list[dict], thumb_sizes: list[str],
                      thumb_max: int, download_dir: Path | None) -> None:
    """ thumbnailLink の寸法・Content-Type・認可要否・向きを確認する（PR0） """
    candidates = select_thumbnail_candidates(all_files, thumb_max)
    if not candidates:
        logger.info('=== thumbnailLink 確認: 対象となる画像が見つかりませんでした ===')
        return

    logger.info('=== thumbnailLink 確認（対象 %d 件 / サイズ指定 %s） ===',
                len(candidates), thumb_sizes)

    # サイズ指定 x 認可有無 の集計
    size_auth_counter: Counter = Counter()  # (size, auth_label, 'ok'|'ng') -> 件数
    orientation_counter: Counter = Counter()

    for f in candidates:
        file_id = f['id']
        meta_list = get_file_metadata(session, file_id)
        if meta_list is None:
            continue

        has_thumb = meta_list.get('hasThumbnail')
        thumb_link = meta_list.get('thumbnailLink')
        drive_meta = meta_list.get('imageMediaMetadata') or {}
        orig_w = drive_meta.get('width')
        orig_h = drive_meta.get('height')
        orig_rotation = drive_meta.get('rotation')

        logger.info('--- id=%s name=%r mimeType=%s hasThumbnail=%s '
                    'imageMediaMetadata(width=%s height=%s rotation=%s) ---',
                    file_id, meta_list.get('name'), meta_list.get('mimeType'),
                    has_thumb, orig_w, orig_h, orig_rotation)

        if not thumb_link:
            logger.warning('  thumbnailLink がありません（hasThumbnail=%s）', has_thumb)
            continue

        base = thumb_link.rsplit('=', 1)[0] if '=' in thumb_link else thumb_link

        # 原本を既にダウンロード済みなら EXIF Orientation を取る（向き判定の材料）
        orig_exif_orientation = None
        orig_pillow_size = None
        if download_dir is not None:
            orig_path = _find_downloaded_original(download_dir, file_id)
            if orig_path is not None:
                info = _open_image_info(orig_path)
                if info is not None:
                    orig_exif_orientation = info['exif_orientation']
                    orig_pillow_size = info['size']

        expected_landscape = _effective_landscape(
            orig_w, orig_h, orig_rotation, orig_exif_orientation)

        for size_spec in thumb_sizes:
            url = f'{base}={size_spec}'
            thumb_dims = None
            for auth_label, use_auth in (('no_auth', False), ('with_auth', True)):
                try:
                    if use_auth:
                        resp = requests_mod.get(
                            url, headers={'Authorization': session.headers.get('Authorization')},
                            timeout=30)
                    else:
                        resp = requests_mod.get(url, timeout=30)
                except Exception as e:
                    logger.warning('  取得失敗 %s (%s): %s',
                                    _redact_thumb_url(url, size_spec), auth_label, e)
                    size_auth_counter[(size_spec, auth_label, 'ng')] += 1
                    continue

                content_type = resp.headers.get('Content-Type', '')
                body_len = len(resp.content) if resp.content else 0
                ok = resp.status_code == 200
                size_auth_counter[(size_spec, auth_label, 'ok' if ok else 'ng')] += 1

                dims = None
                if ok and body_len:
                    try:
                        from PIL import Image
                        from io import BytesIO
                        with Image.open(BytesIO(resp.content)) as im:
                            dims = im.size
                    except Exception as e:
                        logger.warning('  サムネイルを Pillow で開けませんでした: %s', e)

                logger.info(
                    '  %s auth=%s status=%d content-type=%s bytes=%d dims=%s',
                    _redact_thumb_url(url, size_spec), auth_label, resp.status_code,
                    content_type, body_len, dims)

                if use_auth and dims is not None:
                    thumb_dims = dims

                if ok and download_dir is not None and body_len:
                    ext = '.jpg'
                    if 'png' in content_type:
                        ext = '.png'
                    elif 'webp' in content_type:
                        ext = '.webp'
                    dest = download_dir / f'{file_id}__{size_spec}.{auth_label}{ext}'
                    try:
                        dest.write_bytes(resp.content)
                    except OSError as e:
                        logger.warning('  サムネイルの保存に失敗しました: %s', e)

            if thumb_dims is not None and expected_landscape is not None:
                thumb_landscape = None if thumb_dims[0] == thumb_dims[1] else thumb_dims[0] > thumb_dims[1]
                if thumb_landscape is None:
                    match = 'unknown'
                else:
                    match = 'True' if thumb_landscape == expected_landscape else 'False'
            else:
                match = 'unknown'
            orientation_counter[match] += 1

            logger.info(
                '  向き判定 id=%s size=%s: orig(w=%s h=%s rotation=%s) '
                'orig_exif_orientation=%s orig_pillow_size=%s thumb_dims=%s '
                'orientation_match=%s',
                file_id, size_spec, orig_w, orig_h, orig_rotation,
                orig_exif_orientation, orig_pillow_size, thumb_dims, match)

    logger.info('=== thumbnailLink 確認: 集計 ===')
    logger.info('サイズ指定 x 認可有無 別の成功/失敗件数:')
    for (size_spec, auth_label, result) in sorted(size_auth_counter):
        logger.info('  size=%s auth=%s result=%s: %d',
                    size_spec, auth_label, result, size_auth_counter[(size_spec, auth_label, result)])
    logger.info('orientation_match の分布: %s', dict(orientation_counter))


def summarize(all_files: list[dict]) -> None:
    """ 集計をログに出す """
    mime_counter: Counter = Counter(f.get('mimeType', '') for f in all_files)
    logger.info('=== mimeType 別件数 ===')
    for mime, count in mime_counter.most_common():
        logger.info('  %s: %d', mime, count)

    non_folder = [f for f in all_files if f.get('mimeType') != FOLDER_MIME]
    md5_missing = sum(1 for f in non_folder if not f.get('md5Checksum'))
    logger.info('md5Checksum 欠落: %d / %d', md5_missing, len(non_folder))

    metadata_missing_by_mime: Counter = Counter()
    time_samples: list[str] = []
    rotation_counter: Counter = Counter()
    for f in non_folder:
        meta = f.get('imageMediaMetadata')
        if not meta:
            metadata_missing_by_mime[f.get('mimeType', '')] += 1
            continue
        if meta.get('time') and len(time_samples) < 3:
            time_samples.append(repr(meta['time']))
        rotation_counter[meta.get('rotation')] += 1

    logger.info('imageMediaMetadata 欠落（mimeType 別）:')
    for mime, count in metadata_missing_by_mime.most_common():
        logger.info('  %s: %d', mime, count)
    logger.info('time の書式サンプル: %s', time_samples or '（サンプルなし）')
    logger.info('rotation の値の分布: %s', dict(rotation_counter))

    dot_ids = [f['id'] for f in all_files if '.' in f.get('id', '')]
    logger.info("fileId に '.' を含む件数: %d", len(dot_ids))

    unsafe_ids = [f['id'] for f in all_files if not SAFE_ID_PATTERN.match(f.get('id', ''))]
    logger.info('fileId の文字集合が ^[A-Za-z0-9_-]+$ に合わない件数: %d', len(unsafe_ids))
    if unsafe_ids:
        logger.info('  該当 fileId の例（先頭3件）: %s', unsafe_ids[:3])

    shortcut_count = sum(1 for f in all_files if f.get('shortcutDetails'))
    logger.info('ショートカット件数: %d', shortcut_count)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Google Drive の共有フォルダを SA で列挙し、実レスポンスの形を確かめる')
    parser.add_argument('--key', default=os.environ.get('GDRIVE_SA_KEY_FILE'),
                         help='SA の JSON 鍵ファイルパス（既定: 環境変数 GDRIVE_SA_KEY_FILE）')
    parser.add_argument('--root', default=os.environ.get('GDRIVE_ROOT_FOLDER_ID'),
                         help='ルートフォルダの ID（既定: 環境変数 GDRIVE_ROOT_FOLDER_ID）')
    parser.add_argument('--download-dir', default=None,
                         help='指定時のみ原本を保存するディレクトリ')
    parser.add_argument('--max-download', type=int, default=2,
                         help='ダウンロードする画像の最大枚数（既定: 2）')
    parser.add_argument('--thumb-sizes', default=DEFAULT_THUMB_SIZES,
                         help='thumbnailLink に付けて試すサイズ指定のカンマ区切り '
                              f'（既定: {DEFAULT_THUMB_SIZES}）')
    parser.add_argument('--thumb-max', type=int, default=DEFAULT_THUMB_MAX,
                         help='thumbnailLink を確認する画像の最大件数（既定: '
                              f'{DEFAULT_THUMB_MAX}）。rotation が 0 以外 / '
                              'HEIC・HEIF / JPEG の優先順で選ぶ')
    args = parser.parse_args()

    _try_import_deps()
    import requests

    if not args.key:
        logger.error('--key または環境変数 GDRIVE_SA_KEY_FILE で SA 鍵ファイルを指定してください')
        return 1
    if not args.root:
        logger.error('--root または環境変数 GDRIVE_ROOT_FOLDER_ID でルートフォルダIDを指定してください')
        return 1
    if not Path(args.key).is_file():
        logger.error('鍵ファイルが見つかりません: %s', args.key)
        return 1

    try:
        token, expiry = get_access_token(args.key)
    except Exception:
        logger.exception('トークン取得に失敗しました')
        return 1

    session = requests.Session()
    session.headers['Authorization'] = f'Bearer {token}'

    logger.info('ルートフォルダを列挙します: %s', args.root)
    root_entries = list_folder(session, args.root)
    if not root_entries:
        logger.warning('ルート直下に何もありません（共有漏れの可能性があります）')

    subfolders = [f for f in root_entries if f.get('mimeType') == FOLDER_MIME]
    root_files = [f for f in root_entries if f.get('mimeType') != FOLDER_MIME]

    logger.info('=== ルート直下 ===')
    logger.info('サブフォルダ: %d 件 / ファイル: %d 件', len(subfolders), len(root_files))
    for f in root_files:
        logger.info('  file id=%s name=%r mimeType=%s size=%s md5=%s imageMediaMetadata=%s',
                    f.get('id'), f.get('name'), f.get('mimeType'), f.get('size'),
                    '有' if f.get('md5Checksum') else '無',
                    repr(f.get('imageMediaMetadata')))

    all_files = list(root_entries)
    for sub in subfolders:
        logger.info('=== サブフォルダ %r (%s) ===', sub.get('name'), sub.get('id'))
        sub_entries = list_folder(session, sub['id'])
        for f in sub_entries:
            logger.info('  file id=%s name=%r mimeType=%s size=%s md5=%s imageMediaMetadata=%s',
                        f.get('id'), f.get('name'), f.get('mimeType'), f.get('size'),
                        '有' if f.get('md5Checksum') else '無',
                        repr(f.get('imageMediaMetadata')))
        all_files.extend(sub_entries)

    summarize(all_files)

    if args.download_dir:
        dl_dir = Path(args.download_dir)
        dl_dir.mkdir(parents=True, exist_ok=True)

        candidates = [
            f for f in all_files
            if f.get('mimeType') != FOLDER_MIME
            and str(f.get('mimeType', '')).startswith(IMAGE_MIME_PREFIXES)
        ]
        # JPEG / HEIC・HEIF を優先する（対応予定の形式を先に確かめるため）
        candidates.sort(key=lambda f: 0 if 'jpeg' in f.get('mimeType', '') else 1)

        logger.info('=== ダウンロード（最大 %d 件） ===', args.max_download)
        for f in candidates[:args.max_download]:
            ext = '.jpg' if 'jpeg' in f['mimeType'] else '.heic'
            dest = dl_dir / f"{f['id']}{ext}"
            try:
                elapsed, size = download_file(session, f['id'], dest)
                logger.info('  id=%s name=%r: %.3fs %d バイト -> %s',
                            f['id'], f.get('name'), elapsed, size, dest)
                meta = f.get('imageMediaMetadata') or {}
                inspect_downloaded(dest, meta.get('rotation'))
            except requests.HTTPError as e:
                resp = e.response
                logger.error('  ダウンロード失敗 id=%s: HTTP %d %s',
                            f['id'], resp.status_code, resp.text[:500])
            except Exception:
                logger.exception('  ダウンロード中に例外が発生しました: id=%s', f['id'])
        if not candidates:
            logger.info('  対象となる画像（JPEG/HEIC/HEIF）が見つかりませんでした')

    thumb_sizes = [s.strip() for s in args.thumb_sizes.split(',') if s.strip()]
    thumb_download_dir = Path(args.download_dir) if args.download_dir else None
    if thumb_download_dir is not None:
        thumb_download_dir.mkdir(parents=True, exist_ok=True)
    probe_thumbnails(session, requests, all_files, thumb_sizes, args.thumb_max,
                      thumb_download_dir)

    return 0


if __name__ == '__main__':
    _exit_code = 1
    try:
        _exit_code = main()
    except SystemExit as e:
        _exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        logger.exception('想定外の例外により終了します')
        _exit_code = 1
    finally:
        logger.info(DONE_MARKER)
    sys.exit(_exit_code)
