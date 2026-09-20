"""
性能検証用ワンショットスクリプト: ディスクキャッシュの全量充填

現在の写真ソース（settings.json の source / album_id）に含まれる写真を
すべてディスクキャッシュへ充填する。

「fps の落ち込みはキャッシュ未充填の初回一巡だけの現象である」という仮説を
検証するため、定常状態（全枚数がキャッシュ済み）を人為的に作るのが役割。
アプリ本体のコードではない（`.claude/architecture.md` 参照）。

**pygame は import しない。** SDL の初期化が不要な処理のみを行うため、
実機でディスプレイの無い状態からでも実行できる。

実行方法（リポジトリルートから）:

    python tools/verification/warm_cache.py

実機では Cloudflare Tunnel の切断を避けるため、切り離して実行しログを回収する
（`.claude/workflows.md` の「長い処理は切り離して実行する」を参照）。

    setsid nohup python tools/verification/warm_cache.py > warm_cache.log 2>&1 < /dev/null &
    grep -q WARM_ALLDONE warm_cache.log
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# リポジトリルートを sys.path へ足す。immich_probe.py 等の既存検証スクリプトと違い
# このスクリプトは tools/verification/ から `python tools/verification/warm_cache.py`
# として実行される想定のため、リポジトリルート（このファイルの2階層上）を追加する。
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# main.py の logging 設定に合わせる（フォーマット・レベルとも統一する）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('warm_cache')

# main.py と同様、import 前に SDL 関連の環境変数へは一切触れない
# （このスクリプトは pygame を import しないため不要）
from src.config_manager import ConfigManager  # noqa: E402
from src.immich_api import ImmichAPI  # noqa: E402
from src.photo_cache import DEFAULT_DISPLAY_SIZE, PhotoCache  # noqa: E402
from src.photo_source import PhotoSource  # noqa: E402

# 進捗ログを出す間隔（枚数）
PROGRESS_INTERVAL = 10

# 完了判定に使うマーカー。実機で setsid nohup により切り離して実行するため、
# 回収側はこの文字列をログから grep する（.claude/workflows.md 参照）
DONE_MARKER = 'WARM_ALLDONE'


def _parse_display_size(raw: str) -> tuple[int, int]:
    """ "WxH" 形式の文字列を (width, height) へ変換する。argparse の type に渡す """
    try:
        width_str, height_str = raw.lower().split('x', 1)
        width, height = int(width_str), int(height_str)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f'"WxH" 形式で指定してください（例: 1920x1080）: {raw!r}') from e
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError(f'幅・高さは正の整数にしてください: {raw!r}')
    return width, height


def main() -> int:
    parser = argparse.ArgumentParser(
        description='現在の写真ソースの写真をディスクキャッシュへ全量充填する')
    parser.add_argument(
        '--display-size', type=_parse_display_size, default=None, metavar='WxH',
        help='キャッシュを焼くサイズ（例: 1920x1080）。未指定時は実機の既定値 '
             f'{DEFAULT_DISPLAY_SIZE[0]}x{DEFAULT_DISPLAY_SIZE[1]} を使う')
    args = parser.parse_args()

    config = ConfigManager()

    # display_size はアプリ本体（main.py）が renderer.size から取るのに対し、
    # このスクリプトは SDL を持たないため実行環境の解像度を自動取得できない。
    # 解像度を変えた環境で既定値のまま焼くと表示解像度と一致しない
    # キャッシュができてしまうため、--display-size で明示的に指定できるようにする
    # （UI の解像度スケーリング対応。.claude/plans/peaceful-giggling-parrot.md）。
    display_size = args.display_size if args.display_size is not None else DEFAULT_DISPLAY_SIZE
    if args.display_size is None:
        logger.info('--display-size 未指定のため既定値を使います: %dx%d', *display_size)
    else:
        logger.info('指定された解像度でキャッシュを焼きます: %dx%d', *display_size)
    cache = PhotoCache(config, display_size=display_size)

    try:
        api = ImmichAPI(config)
    except ValueError as e:
        logger.error('Immich に接続できません: %s', e)
        return 1

    source = PhotoSource(config, api, cache)

    logger.info('写真リストを取得します: source=%s album_id=%s',
                config.get('source'), config.get('album_id'))
    photos = source.load_list(force=True)
    if not photos:
        logger.error('写真リストが空です。キャッシュ充填を行えません')
        return 1

    total = len(photos)
    logger.info('写真リストを取得しました: %d 件', total)

    hit_count = 0
    fetched_count = 0
    failed_count = 0
    fetch_durations: list[float] = []

    for i, photo in enumerate(photos, start=1):
        # 1枚の異常（想定外のレスポンス形・ensure_photo 内の例外等）で
        # 残り全部が止まらないよう、写真単位で握り取って次へ進む。
        # 握り潰さず必ずログに残す（.claude/coding-style.md）。
        try:
            asset_id = photo['id']

            already_cached = cache.get_photo_path(asset_id) is not None

            start = time.monotonic()
            path = source.ensure_photo(asset_id)
            elapsed = time.monotonic() - start

            if path is None:
                failed_count += 1
                logger.error('取得失敗: id=...%s elapsed=%.3fs', str(asset_id)[-6:], elapsed)
            elif already_cached:
                hit_count += 1
                logger.info('キャッシュヒット: id=...%s elapsed=%.3fs', str(asset_id)[-6:], elapsed)
            else:
                fetched_count += 1
                fetch_durations.append(elapsed)
                logger.info('新規取得: id=...%s elapsed=%.3fs', str(asset_id)[-6:], elapsed)
        except Exception:
            failed_count += 1
            logger.exception('写真の処理中に例外が発生しました（次へ進みます）: index=%d', i)

        if i % PROGRESS_INTERVAL == 0:
            logger.info('進捗: %d/%d 完了', i, total)

    logger.info('進捗: %d/%d 完了', total, total)

    logger.info('=== サマリ ===')
    logger.info('総数=%d 新規取得=%d キャッシュヒット=%d 失敗=%d',
                total, fetched_count, hit_count, failed_count)

    if fetch_durations:
        logger.info(
            '新規取得の所要時間(秒): 合計=%.3f 平均=%.3f 最小=%.3f 最大=%.3f',
            sum(fetch_durations),
            sum(fetch_durations) / len(fetch_durations),
            min(fetch_durations),
            max(fetch_durations),
        )
    else:
        logger.info('新規取得の所要時間(秒): 新規取得なし')

    total_size_mb = cache.get_total_size() / (1024 * 1024)
    logger.info('キャッシュ総容量: %.2f MB', total_size_mb)

    return 0 if failed_count == 0 else 1


if __name__ == '__main__':
    # 実機では setsid nohup で切り離して実行し、回収側はログ末尾の DONE_MARKER を
    # grep して完了を判定する（.claude/workflows.md）。main() のどこで例外が
    # 出てもマーカーだけは必ず出す必要があるため、出力箇所をここ1箇所に集約する
    # （main() 内の複数箇所からは出さない。二重出力ではなく単一化のため）。
    _exit_code = 1
    try:
        _exit_code = main()
    except SystemExit as e:
        # argparse の --help / 引数エラーは SystemExit で抜けてくる。
        # 想定外の例外として握りつぶすと --help が常に終了コード1になってしまうため、
        # argparse が決めた終了コードをそのまま使う。
        _exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        logger.exception('想定外の例外により終了します')
        _exit_code = 1
    finally:
        logger.info(DONE_MARKER)
    sys.exit(_exit_code)
