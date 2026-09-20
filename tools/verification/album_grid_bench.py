"""
性能検証用ワンショットスクリプト: アルバム選択画面のグリッド性能

**未検証事項「アルバム数が多い場合のグリッド性能」を実測で潰すためのもの。**
実機の Immich にはアルバムが 13 件しかないため、実アルバムを循環複製して
任意件数（数百件）を合成し、本物の `Renderer` と `AlbumScreen` を使って
スクロール・描画・入力応答を機械的に駆動する。

アプリ本体のコードではない（`.claude/architecture.md` 参照）。
**本番コードには一切手を入れずに測る**のがこのスクリプトの前提で、そのために
`AlbumScreen` の非公開メンバ（`_scroll` / `_cells` / `_textures` 等）を直接読む。
計測用スクリプトに限った割り切りであり、本番コードで真似してはならない。

測るのは次の4点。

1.  スクロール中の fps（フレーム時間の中央値・下位10%・最悪値）
2.  常駐テクスチャ数とメモリ（サムネイル / アルバム名の Text / VmRSS・VmHWM）
3.  画面を開くまでの時間（アルバム取得 と グリッド構築 を分けて計測）
4.  入力応答のレイテンシ（`handle_input` の所要時間。全子走査が効くか）

**Immich も本番のキャッシュも汚さない。** サムネイルは実アセットを本当に
ダウンロードするが、保存先は `--cache-dir`（既定 `./bench-cache`）で分離する。
`AlbumScreen` は取得したアルバム一覧で `PhotoCache.cleanup_thumbnails()` を
呼ぶため、**本番のキャッシュディレクトリを指すと本番のサムネイルが消える。**
既定値を本番と別にしてあるのはこの事故を防ぐためで、`--cache-dir` に
`/cache` を渡してはならない。

実行方法（リポジトリルートから）:

    python tools/verification/album_grid_bench.py --count 300

**件数ごとにプロセスを分けること。** 同一プロセスで `on_leave()` -> `on_enter()` を
繰り返すと、前の件数で作った Text テクスチャや Python オブジェクトが残った状態を
測ることになり、件数と常駐量の対応が取れなくなる。実機での一括実行は
`tools/verification/album_bench_runner.sh` を使う。

実機では Cloudflare Tunnel の切断を避けるため切り離して実行し、ログを回収する
（`.claude/workflows.md` の「長い処理は切り離して実行する」を参照）。
完了マーカーは `ALBUMBENCH_ALLDONE`。
"""

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# リポジトリルートを sys.path へ足す（warm_cache.py と同じ理由・同じ書き方）
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# main.py の logging 設定に合わせる
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('album_grid_bench')

# main.py と同様、import 前に SDL 関連の環境変数へは触れない
# （コンテナの ENV / docker run の -e で与えられている前提）
import pygame as pg  # noqa: E402

from src.config_manager import ConfigManager  # noqa: E402
from src.gui.renderer import TAP_DOWN, TAP_MOVE, TAP_UP, Renderer  # noqa: E402
from src.gui.screens.album import GRID_COLUMNS, HEADER_HEIGHT, AlbumScreen  # noqa: E402
from src.immich_api import ImmichAPI  # noqa: E402
from src.photo_cache import PhotoCache  # noqa: E402

DONE_MARKER = 'ALBUMBENCH_ALLDONE'

# 実機の垂直同期（49.61Hz）。fps の判定基準に使う
VSYNC_HZ = 49.61

# スワイプ1回の設定。ScrollView は慣性を持たず scroll_y = 開始値 - dy なので、
# 1回のスワイプで動く量は指の移動距離そのものになる。長いリストを端まで
# 送るには複数回スワイプする必要がある。
SWIPE_STEP_PX = 20          # 1フレームあたりの移動量（閾値 12px を必ず超える大きさ）
SWIPE_FRAMES = 20           # 1スワイプのフレーム数 -> 400px 動く
SWIPE_LIMIT = 400           # 無限ループ防止の上限（端に着かない場合の保険）


def read_proc_status_kb(key: str) -> int:
    """ /proc/self/status から任意の項目（VmRSS / VmHWM）を kB で読む """
    try:
        with open('/proc/self/status', encoding='utf-8') as f:
            for line in f:
                if line.startswith(key + ':'):
                    return int(line.split()[1])
    except OSError:
        pass
    return -1


class SyntheticAlbumAPI(ImmichAPI):
    """
    実アルバムを循環複製して任意件数を合成するスタブ API

    差し替えるのは `fetch_albums()` だけ。`albumThumbnailAssetId` は実物の
    アセット ID をそのまま引き継ぐため、`download_asset()` は本物の Immich から
    実際のサムネイルを取得する（テクスチャ生成まで本番と同じ経路を通す）。

    **合成アルバムは `id` だけが異なり、サムネイルのアセット ID は重複する。**
    `PhotoCache` のサムネイルは (album_id, thumbnail_id) をキーにするため、
    同じアセットを件数の分だけダウンロードすることになる。実運用より通信量は
    多いが、「N 件分のサムネイルをテクスチャ化する」負荷は実運用と等価になる。
    """

    def __init__(self, config: ConfigManager, count: int, long_name_every: int = 3) -> None:
        super().__init__(config)
        self._count = count
        self._long_name_every = long_name_every
        # 実 API の所要時間（合成のコストは含めない）
        self.real_fetch_seconds = 0.0
        self.real_album_count = 0

    def fetch_albums(self) -> list[dict[str, Any]]:
        started = time.perf_counter()
        real = super().fetch_albums()
        self.real_fetch_seconds = time.perf_counter() - started
        self.real_album_count = len(real)

        if not real:
            logger.error('実アルバムが 0 件のため合成できません（Immich への接続を確認すること）')
            return []

        synthesized: list[dict[str, Any]] = []
        for i in range(self._count):
            base = real[i % len(real)]
            name = f'{base.get("albumName") or "(名前なし)"} #{i + 1:04d}'
            if self._long_name_every > 0 and i % self._long_name_every == 0:
                # 折り返し2行になる長い名前を一定割合で混ぜる。1行しか無い名前ばかりだと
                # wrap_lines() の二分探索と Text テクスチャの本数が実運用より軽く出る
                name += ' 折り返し確認用の長いアルバム名テキスト'
            synthesized.append({
                'id': f'bench{i:05d}-{base.get("id")}',
                'albumName': name,
                'albumThumbnailAssetId': base.get('albumThumbnailAssetId'),
                'assetCount': base.get('assetCount', 0),
            })

        logger.info('アルバムを合成しました: 実 %d 件 -> 合成 %d 件（実 API %.3f 秒）',
                    len(real), len(synthesized), self.real_fetch_seconds)
        return synthesized


class FrameDriver:
    """ 1フレーム分（入力 -> update -> draw -> present）を回してフレーム時間を返す """

    def __init__(self, renderer: Renderer, screen: AlbumScreen) -> None:
        self._r = renderer
        self._screen = screen
        self.frame_times: list[float] = []
        # サムネイルテクスチャの常駐ピーク。スワイプ単位で見ると山を跨いで
        # 取りこぼすため、毎フレーム観測する（dict の len なので安価）
        self.peak_thumbs = 0

    def step(self, inputs: tuple = ()) -> float:
        """
        1フレーム進める。戻り値は所要秒。

        `present()` は vsync でブロックするため、この所要秒がそのまま
        「実際に表示されているフレームの間隔」になる（9-4 / 9-10 の教訓により、
        vsync を切って測った値は根拠にしない）。
        """
        started = time.perf_counter()
        # SDL のイベントキューを溜めない。main.py は event.get() で吸っている
        pg.event.pump()
        pg.event.clear()
        for kind, x, y in inputs:
            self._screen.handle_input(kind, x, y)
        self._screen.update(time.perf_counter())
        self._r.clear()
        self._screen.draw()
        self._r.present()
        elapsed = time.perf_counter() - started
        self.frame_times.append(elapsed)
        self.peak_thumbs = max(self.peak_thumbs, len(self._screen._textures))
        return elapsed


def summarize_frames(times: list[float]) -> str:
    """ フレーム時間の一覧を fps の統計へ落とす """
    if not times:
        return 'サンプルなし'
    ordered = sorted(times)
    median = statistics.median(ordered)
    # 「下位10%」= 遅い方から10%の位置（fps が低い側）
    slow_index = max(0, int(len(ordered) * 0.9) - 1)
    slow = ordered[slow_index]
    worst = ordered[-1]
    below_45 = sum(1 for t in times if t > 1.0 / 45.0)
    return (f'frames={len(times)} '
            f'fps中央値={1.0 / median:.1f} '
            f'fps下位10%={1.0 / slow:.1f} '
            f'fps最悪={1.0 / worst:.1f} '
            f'45fps未満={below_45}件')


class GridBench:
    """ アルバム選択画面を機械的に駆動して各指標を集める """

    def __init__(self, renderer: Renderer, screen: AlbumScreen, count: int) -> None:
        self._r = renderer
        self._screen = screen
        self._count = count
        self._driver = FrameDriver(renderer, screen)

    # ------------------------------------------------------------ 内部状態の観測

    def _resident(self) -> tuple[int, int]:
        """ (サムネイルテクスチャ数, アルバム名 Text の本数) を返す """
        thumbs = len(self._screen._textures)
        labels = sum(len(cell._label_lines) for cell in self._screen._cells)
        return thumbs, labels

    def log_resident(self, tag: str) -> None:
        thumbs, labels = self._resident()
        logger.info('[%s] 常駐: サムネイル=%d枚 アルバム名Text=%d本 VmRSS=%dkB VmHWM=%dkB',
                    tag, thumbs, labels, read_proc_status_kb('VmRSS'),
                    read_proc_status_kb('VmHWM'))

    # ------------------------------------------------------------------ 開く

    def open_screen(self) -> bool:
        """
        `on_enter()` からグリッド構築完了までを計測する。

        アルバム取得（ワーカースレッド）とグリッド構築（メインスレッド）は
        別物なので分けて記録する。構築は `_loading` を False にする
        `update()` 呼び出しの中で起きるため、その1回の所要時間を構築時間とみなす。
        """
        started = time.perf_counter()
        self._screen.on_enter()

        build_seconds = 0.0
        while self._screen._loading:
            if time.perf_counter() - started > 120.0:
                logger.error('アルバム一覧の取得が 120 秒で終わりませんでした')
                return False
            before = time.perf_counter()
            self._driver.step()
            if not self._screen._loading:
                # このフレームの update() の中で _build_grid() が走った。
                # draw/present も含む値なので、フレーム全体の時間として報告する
                build_seconds = time.perf_counter() - before

        total = time.perf_counter() - started
        scroll = self._screen._scroll
        logger.info('画面を開くまで: 合計=%.3f秒 グリッド構築フレーム=%.3f秒 '
                    'セル=%d個 content_height=%dpx max_scroll=%dpx',
                    total, build_seconds, len(self._screen._cells),
                    scroll.content_height if scroll else -1,
                    scroll.max_scroll if scroll else -1)
        # 開いた直後は先読み分のテクスチャしか無いはず。基準値として残す
        self.log_resident('開いた直後')
        return True

    # ---------------------------------------------------------------- スクロール

    def _swipe(self, from_y: int, direction: int) -> list[float]:
        """
        1回スワイプする。`direction` は -1 で下方向へ送る（内容が上へ動く）。

        DOWN -> MOVE を SWIPE_FRAMES 回 -> UP を、1フレームに1イベントずつ流す。
        実機のタッチも1フレームに複数の MOVE が溜まることがあるが、ここでは
        「指の動きを一定速度で与えたときに描画が追いつくか」を見たいので等間隔にする。
        """
        width, _height = self._r.size
        x = width // 2
        times = [self._driver.step(((TAP_DOWN, x, from_y),))]
        y = from_y
        for _ in range(SWIPE_FRAMES):
            y += SWIPE_STEP_PX * direction
            times.append(self._driver.step(((TAP_MOVE, x, y),)))
        times.append(self._driver.step(((TAP_UP, x, y),)))
        return times

    def scroll_pass(self, tag: str, idle_frames: int) -> list[float]:
        """ 先頭 -> 末尾 -> 先頭 を1往復する。戻り値はこの区間のフレーム時間 """
        scroll = self._screen._scroll
        if scroll is None:
            return []

        _width, height = self._r.size
        bottom_y = height - 60
        top_y = HEADER_HEIGHT + 60
        times: list[float] = []
        self._driver.peak_thumbs = len(self._screen._textures)

        for direction, limit_check in ((-1, lambda: scroll.scroll_y >= scroll.max_scroll),
                                       (1, lambda: scroll.scroll_y <= 0)):
            start_y = bottom_y if direction < 0 else top_y
            swipes = 0
            while not limit_check() and swipes < SWIPE_LIMIT:
                times.extend(self._swipe(start_y, direction))
                for _ in range(idle_frames):
                    # サムネイルワーカーの結果を取り込む余地を作る
                    times.append(self._driver.step())
                swipes += 1
            logger.info('[%s] %s方向: スワイプ%d回 scroll_y=%d/%d',
                        tag, '送り' if direction < 0 else '戻し',
                        swipes, scroll.scroll_y, scroll.max_scroll)

        logger.info('[%s] %s', tag, summarize_frames(times))
        logger.info('[%s] 往復中のサムネイル常駐ピーク=%d枚', tag, self._driver.peak_thumbs)
        self.settle(tag)
        self.log_resident(tag + '・往復後')
        return times

    def settle(self, tag: str, timeout: float = 90.0, extra_frames: int = 30) -> None:
        """
        サムネイル取得の在庫が捌けるまで無操作フレームを回す。

        **これが無いと常駐テクスチャ数を過小評価する。** 取得ワーカーは直列で
        1枚ずつ処理するため、スワイプが速いと往復が終わっても結果が届いておらず、
        「可視範囲にあるのにテクスチャが無い」状態のまま数えてしまう
        （階層1のスモークで実際に 0 枚と出た）。
        """
        started = time.perf_counter()
        while (self._screen._thumb_request_queue.qsize()
               or self._screen._thumb_result_queue.qsize()):
            if time.perf_counter() - started > timeout:
                logger.warning('[%s] サムネイルの在庫が %.0f 秒で捌けませんでした（未処理=%d件）',
                               tag, timeout, self._screen._thumb_request_queue.qsize())
                break
            self._driver.step()
        # キューが空でも最後の1枚がダウンロード中でありうるので少し回す
        for _ in range(extra_frames):
            self._driver.step()

    # -------------------------------------------------------------- 入力レイテンシ

    def measure_tap_latency(self, samples: int = 5) -> None:
        """
        タップから選択反映までの時間を測る。

        `ScrollView.handle_down()` は全子を走査するため、ここが件数に比例する。
        選択自体は `handle_input(TAP_UP)` の中で同期的に確定するので、
        画面に出るまでは常に「その次の1フレーム」になる。したがって
        測るべきは `handle_input` そのものの所要時間である。
        """
        scroll = self._screen._scroll
        if scroll is None or not self._screen._cells:
            return

        # **列の中心を実セルから取る。** 画面中央 x=512 は列と列の隙間
        # （GRID_GAP=16px）にちょうど落ち、どのセルにも当たらない。
        # 階層1のスモークで「選択が変化 0/5」になった原因がこれだった。
        sample_cell = self._screen._cells[min(2, len(self._screen._cells) - 1)]
        x = sample_cell.rect.centerx
        row_height = self._screen._row_height
        cell_size = self._screen._cell_size
        down_ms: list[float] = []
        up_ms: list[float] = []
        changed = 0

        for i in range(samples):
            # 位置をずらしながら測る。端に着いていると scroll_y が動かないので
            # 途中位置へ移動してから測る
            target = int(scroll.max_scroll * (i + 1) / (samples + 1))
            scroll.scroll_y = max(0, min(scroll.max_scroll, target))
            # 可視範囲の中で「行の中身」に当たる画面 y を求める。
            # content_y = scroll_y + (y - viewport.y) の関係を逆に解く。
            row = scroll.scroll_y // row_height + 1
            y = int(row * row_height + cell_size // 2 - scroll.scroll_y) + HEADER_HEIGHT
            self._driver.step()

            before = self._screen._selected_index
            t0 = time.perf_counter()
            self._screen.handle_input(TAP_DOWN, x, y)
            down_ms.append((time.perf_counter() - t0) * 1000.0)

            self._driver.step()

            t0 = time.perf_counter()
            self._screen.handle_input(TAP_UP, x, y)
            up_ms.append((time.perf_counter() - t0) * 1000.0)

            # 反映されたフレームを1枚描く（表示までは常に1フレーム）
            self._driver.step()
            if self._screen._selected_index != before:
                changed += 1

        logger.info('入力応答: DOWN=%.3fms(最大%.3f) UP=%.3fms(最大%.3f) '
                    '選択が変化=%d/%d 表示までのフレーム=1',
                    statistics.mean(down_ms), max(down_ms),
                    statistics.mean(up_ms), max(up_ms), changed, samples)


def prefill_thumbnails(api: SyntheticAlbumAPI, cache: PhotoCache) -> None:
    """
    合成アルバム全件分のサムネイルをディスクキャッシュへ先に置く。

    **合成アルバムは id だけが違い、サムネイルのアセット ID は実アルバムの分しか
    無い**（実 16 件なら 16 種類）。`PhotoCache` は (album_id, thumbnail_id) を
    キーにするため素直に取ると件数分だけ通信するが、実体は同じアセットなので
    種類ごとに1回だけダウンロードし、残りは同じバイト列から書き出す。

    これが無いと「定常状態のスクロール性能」を測れない。取得ワーカーは直列で
    1枚あたり約1秒かかるため（階層1の実測）、300件では往復の間ずっと
    取得待ちのまま終わってしまう。
    """
    entries = api.fetch_albums()  # 合成は決定的なので on_enter が取るものと同じ
    raw_by_asset: dict[str, bytes | None] = {}
    stored = hit = failed = 0
    started = time.perf_counter()

    for entry in entries:
        album_id = entry.get('id')
        thumb_id = entry.get('albumThumbnailAssetId')
        if not album_id or not thumb_id:
            continue
        if cache.get_thumbnail_path(album_id, thumb_id) is not None:
            hit += 1
            continue
        if thumb_id not in raw_by_asset:
            raw_by_asset[thumb_id] = api.download_asset(thumb_id, size='thumbnail')
        raw = raw_by_asset[thumb_id]
        if raw is None or cache.store_thumbnail(album_id, thumb_id, raw) is None:
            failed += 1
            continue
        stored += 1

    logger.info('サムネイルを事前充填しました: 新規=%d件 既存=%d件 失敗=%d件 '
                '実ダウンロード=%d種類 所要=%.1f秒',
                stored, hit, failed, len(raw_by_asset), time.perf_counter() - started)


def main() -> int:
    parser = argparse.ArgumentParser(description='アルバム選択画面のグリッド性能ベンチ')
    parser.add_argument('--count', type=int, default=300,
                        help='合成するアルバム件数（既定 300）')
    parser.add_argument('--passes', type=int, default=2,
                        help='スクロール往復の回数。1周目はサムネイル取得を含むため '
                             '2周目以降が定常状態になる（既定 2）')
    parser.add_argument('--idle-frames', type=int, default=3,
                        help='スワイプの合間に挟む無操作フレーム数（既定 3）')
    parser.add_argument('--cache-dir', default='./bench-cache',
                        help='ベンチ専用のキャッシュディレクトリ。'
                             '**本番の /cache を指してはならない**（cleanup_thumbnails が '
                             '本番のサムネイルを削除するため）')
    parser.add_argument('--prefill', action=argparse.BooleanOptionalAction, default=True,
                        help='測定前にサムネイルをディスクキャッシュへ充填する（既定 有効）。'
                             '--no-prefill にすると初回訪問（取得待ちを含む）の測定になる')
    parser.add_argument('--config-dir', default=None,
                        help='設定ディレクトリ。未指定なら PF_CONFIG_DIR / ./config')
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir).resolve()
    if cache_dir.name == 'cache' and str(cache_dir) == '/cache':
        logger.error('--cache-dir に本番のキャッシュ（/cache）は指定できません')
        return 1

    settings_path = Path(args.config_dir) / 'settings.json' if args.config_dir else None
    config = ConfigManager(settings_path)
    logger.info('設定=%s キャッシュ=%s 件数=%d 往復=%d',
                config.settings_path, cache_dir, args.count, args.passes)

    renderer = Renderer()
    renderer.create()

    cache = PhotoCache(config, cache_dir=cache_dir, display_size=renderer.size)
    api = SyntheticAlbumAPI(config, args.count)
    screen = AlbumScreen(renderer, config, api, cache)
    bench = GridBench(renderer, screen, args.count)

    logger.info('開始時のメモリ: VmRSS=%dkB VmHWM=%dkB',
                read_proc_status_kb('VmRSS'), read_proc_status_kb('VmHWM'))

    if args.prefill:
        prefill_thumbnails(api, cache)

    try:
        if not bench.open_screen():
            return 1
        logger.info('グリッド: %d列 セル=%d個 (合成 %d 件 + 仮想エントリ)',
                    GRID_COLUMNS, len(screen._cells), args.count)

        for i in range(args.passes):
            cold = (i == 0 and not args.prefill)
            tag = f'{i + 1}周目' + ('（サムネイル取得を含む）' if cold else '（定常）')
            bench.scroll_pass(tag, args.idle_frames)

        bench.measure_tap_latency()
    finally:
        screen.on_leave()
        renderer.destroy()

    logger.info('終了時のメモリ: VmRSS=%dkB VmHWM=%dkB',
                read_proc_status_kb('VmRSS'), read_proc_status_kb('VmHWM'))
    logger.info('全フレーム通算: %s', summarize_frames(bench._driver.frame_times))
    logger.info('件数=%d の測定を終了しました', args.count)
    return 0


if __name__ == '__main__':
    # warm_cache.py と同じ方針。main() のどこで例外が出てもマーカーだけは必ず出す
    _exit_code = 1
    try:
        _exit_code = main()
    except BaseException:
        logger.exception('想定外の例外により終了します')
        _exit_code = 1
    finally:
        logger.info(DONE_MARKER)
    sys.exit(_exit_code)
