"""
画像1枚のデコードのピークメモリと所要時間を測る（PR0 / PoC）

`.claude/plans/abundant-weaving-kernighan.md` PR0 の通過条件
（HEIC 12MP のピークメモリと fps への影響が許容範囲に収まること）を確かめるための
ワンショット。**1プロセス1ケースで呼ぶこと。** maxrss は一度上がると同一プロセス内では
下がらないため、複数ケースを1プロセスで測ると後のケースの値が前のケースに引きずられる。

pillow-heif は Dev Container にもコンテナイメージにも入っていない。`--no-heif` を
付けない限り import を試み、失敗時は分かりやすいメッセージで終了する。

実行方法（リポジトリルートから。依存を PYTHONPATH で渡す想定）:

    PYTHONPATH=/path/to/site python tools/verification/heic_decode_bench.py \\
        photo.heic --mode decode --target 1024x600

手元の JPEG だけで Pillow 標準の経路を測る場合（Dev Container で動く）:

    python tools/verification/heic_decode_bench.py photo.jpg --no-heif
"""

import argparse
import json
import logging
import os
import resource
import sys
import time
from io import BytesIO
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('heic_decode_bench')

# 完了判定に使うマーカー（.claude/workflows.md「長い処理は切り離して実行する」参照）
DONE_MARKER = 'HEIC_BENCH_ALLDONE'
# main() の最後に出す1行 JSON のプレフィックス
RESULT_PREFIX = 'BENCH_RESULT'

_START_TIME = time.monotonic()


def _maxrss_kb() -> int:
    """ プロセス開始からの maxrss（KB）。Linux では ru_maxrss は KB 単位 """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _proc_status() -> tuple[str, str]:
    """ /proc/self/status から VmRSS / VmHWM の行をそのまま取る（無ければ空文字） """
    vmrss = vmhwm = ''
    try:
        with open('/proc/self/status', encoding='utf-8') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    vmrss = line.split(':', 1)[1].strip()
                elif line.startswith('VmHWM:'):
                    vmhwm = line.split(':', 1)[1].strip()
    except OSError:
        pass
    return vmrss, vmhwm


def checkpoint(label: str, results: dict, **extra) -> None:
    """ 計測点を1行ログに出し、results へも記録する """
    elapsed = time.monotonic() - _START_TIME
    maxrss = _maxrss_kb()
    vmrss, vmhwm = _proc_status()
    extra_str = ' '.join(f'{k}={v}' for k, v in extra.items())
    logger.info('[%s] elapsed=%.3fs maxrss=%dKB VmRSS=%s VmHWM=%s %s',
                label, elapsed, maxrss, vmrss, vmhwm, extra_str)
    results['checkpoints'][label] = {
        'elapsed_sec': round(elapsed, 3),
        'maxrss_kb': maxrss,
        'vmrss': vmrss,
        'vmhwm': vmhwm,
    }


def _parse_target(raw: str) -> tuple[int, int]:
    try:
        w, h = raw.lower().split('x', 1)
        return int(w), int(h)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f'"WxH" 形式で指定してください: {raw!r}') from e


def main() -> int:
    parser = argparse.ArgumentParser(
        description='画像1枚のデコードのピークメモリと所要時間を測る（1プロセス1ケース）')
    parser.add_argument('image', help='対象の画像ファイルパス')
    parser.add_argument('--mode', choices=('import-only', 'decode'), default='decode',
                        help='import-only は google-auth の import コストだけを測る')
    parser.add_argument('--target', type=_parse_target, default=(1024, 600), metavar='WxH',
                        help='縮小の目標サイズ（既定: 1024x600）')
    parser.add_argument('--no-heif', action='store_true',
                        help='pillow_heif の登録を行わない（JPEG 専用で測るとき）')
    parser.add_argument('--draft', dest='draft', action='store_true', default=True,
                        help='JPEG のとき Image.draft() を使う（既定）')
    parser.add_argument('--no-draft', dest='draft', action='store_false',
                        help='Image.draft() を使わない')
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.is_file():
        logger.error('画像ファイルが見つかりません: %s', image_path)
        return 1

    results: dict = {
        'image': str(image_path),
        'mode': args.mode,
        'target': list(args.target),
        'checkpoints': {},
    }

    logger.info('MALLOC_ARENA_MAX=%s', os.environ.get('MALLOC_ARENA_MAX', '（未設定）'))
    logger.info('python=%s', sys.version.split()[0])

    checkpoint('start', results)

    try:
        from PIL import Image, ImageOps
        import PIL
    except ImportError as e:
        logger.error('Pillow が import できません: %s', e)
        return 2
    logger.info('Pillow=%s', PIL.__version__)
    checkpoint('import_pil', results)

    if args.mode == 'import-only':
        try:
            from google.oauth2.service_account import Credentials  # noqa: F401
            from google.auth.transport.requests import Request  # noqa: F401
            logger.info('google.oauth2 / google.auth の import に成功しました')
        except ImportError as e:
            logger.warning('google-auth 系が import できません（未導入）: %s', e)
        checkpoint('import_google_auth', results)

    if not args.no_heif:
        try:
            from pillow_heif import register_heif_opener
            import pillow_heif
            register_heif_opener()
            logger.info('pillow_heif=%s を登録しました', pillow_heif.__version__)
        except ImportError as e:
            logger.error(
                'pillow_heif が import できません。--no-heif で HEIC を扱わない'
                'モードにするか、tools/verification/gdrive_poc_runner.sh の手順で'
                '依存を用意してください: %s', e)
            return 2
        checkpoint('register_heif', results)
    else:
        logger.info('pillow_heif=（未登録。--no-heif 指定）')
        results['checkpoints']['register_heif'] = None

    if args.mode == 'import-only':
        logger.info('import-only モードのためデコードは行いません')
    else:
        try:
            im = Image.open(image_path)
            im_format = im.format
            im_size = im.size
            im_mode = im.mode
        except Exception:
            logger.exception('Image.open() に失敗しました')
            return 3
        results['source_format'] = im_format
        results['source_size'] = list(im_size)
        checkpoint('open', results, format=im_format, size=im_size, mode=im_mode)

        # DecompressionBombError は握りつぶさず、クラス名と MRO を出して終了する
        # （Image.MAX_IMAGE_PIXELS はいじらない方針。.claude/plans/abundant-weaving-kernighan.md）
        try:
            if args.draft and im_format == 'JPEG':
                target_w, target_h = args.target
                im.draft('RGB', (target_w, target_h))
                checkpoint('draft', results, size=im.size)

            im.load()
            checkpoint('load', results)

            im = ImageOps.exif_transpose(im)
            checkpoint('exif_transpose', results, size=im.size if im else None)

            im.thumbnail(args.target, Image.LANCZOS)
            checkpoint('thumbnail', results, size=im.size)

            buf = BytesIO()
            im.convert('RGB').save(buf, format='JPEG', quality=90)
            checkpoint('encode_jpeg', results, bytes=len(buf.getvalue()))
        except Exception as e:
            mro = [c.__name__ for c in type(e).__mro__]
            if 'DecompressionBombError' in mro:
                logger.error('DecompressionBombError が発生しました: MRO=%s', mro)
                return 3
            logger.exception('デコード処理中に例外が発生しました: MRO=%s', mro)
            return 4

    total_elapsed = time.monotonic() - _START_TIME
    results['total_elapsed_sec'] = round(total_elapsed, 3)

    print(f'{RESULT_PREFIX} {json.dumps(results, ensure_ascii=False)}')
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
