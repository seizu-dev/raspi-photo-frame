import logging
import queue
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pygame as pg

from src.gui import transitions
from src.gui.renderer import TAP_UP
from src.gui.screens import ACTION_MENU

if TYPE_CHECKING:
    from src.config_manager import ConfigManager
    from src.gui.overlay import Overlay
    from src.gui.renderer import Renderer
    from src.photo_source import PhotoSource

logger = logging.getLogger(__name__)

# タッチの横方向の割り当て（photo-frame 踏襲）
ZONE_PREV_RATIO = 0.20
ZONE_NEXT_RATIO = 0.80


class SlideshowScreen:
    """
    写真を切り替えて表示する画面

    **常駐する写真テクスチャは現在と次の最大2枚。** 先読みも次の1枚のみで、
    どちらも .claude/architecture.md の禁止パターンに直接対応する。

    photo-frame は次の画像を `Event.wait(5)` で待っており、間に合わないと
    メインスレッドが最大5秒固まっていた。ここでは待たず、用意できていなければ
    その回の送りを見送る。
    """

    def __init__(self, renderer: 'Renderer', overlay: 'Overlay',
                 config: 'ConfigManager', source: 'PhotoSource') -> None:
        self._r = renderer
        self._overlay = overlay
        self._config = config
        self._source = source

        self._photos: list[dict[str, Any]] = []
        self._index = 0
        self._history: list[int] = []

        # パスを持っておく。SDL を破棄するとテクスチャは無効になるため作り直せるように
        self._current_path: Path | None = None
        self._current_tex = None
        self._next_path: Path | None = None
        self._next_tex = None
        self._gen = -1

        self._fading = False
        self._fade_start = 0.0
        self._last_change = 0.0
        # このフェード中に固定して使う具体的な遷移種類。_begin_fade() で
        # config を読んで決定する（フェード中は config が変わっても揺れない）
        self._transition = transitions.CROSSFADE
        # random の抽選で「直前と同じ種類を避ける」ために持つ、直近に実際に
        # 使った具体的な種類（config の値そのものではなく解決後の値）
        self._last_transition_kind: str | None = None
        # 不正な transition 値で警告ログを出すのを値ごとに1回に絞るための記録
        self._transition_warned: set[str] = set()
        # 同一フレーム内で手動送りを二重に効かせないための印。
        # **描画が長引くと（初回一巡の JPEG デコード中など）、その間に連打された
        # タッチが1フレームにまとめて届き、写真が一気に飛ぶ。** update() の冒頭で戻す
        self._manual_advanced = False

        self._preload_queue: queue.Queue = queue.Queue()
        self._preloading_index: int | None = None
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ 外部 API

    @property
    def photo_count(self) -> int:
        return len(self._photos)

    @property
    def has_photo(self) -> bool:
        return self._current_tex is not None

    def set_photos(self, photos: list[dict[str, Any]]) -> None:
        """ 写真リストを差し替えて最初から始める """
        self._photos = photos
        self._index = 0
        self._history.clear()
        self._drop_textures()
        self._current_path = None
        self._next_path = None
        self._preloading_index = None
        self._drain_queue()
        self._last_change = time.monotonic()
        if photos:
            self._request_load(0, as_current=True)

    def notify_activity(self) -> None:
        """ 手動操作があったので自動送りのタイマーを引き直す """
        self._last_change = time.monotonic()

    def reload_current(self) -> None:
        """
        表示方法（photo_fit）の変更など、同じ写真を別のファイルで作り直す
        必要があるときに呼ぶ。

        取得はワーカースレッド、テクスチャ化は _collect_preloaded()（メインスレッド）
        という既存の境界をそのまま通す。新方式が未キャッシュならダウンロードが
        走るため表示が数秒遅れるが、これは既存の先読み待ちと同じ挙動で正常。
        """
        if not self._photos:
            return
        # フェード中に呼ぶと中途半端な合成が残るため打ち切る
        self._fading = False
        self._drop_next()
        self._preloading_index = None
        self._request_load(self._index, as_current=True)

    def stop(self) -> None:
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()

    # -------------------------------------------------------------------- 更新

    def update(self, now: float, allow_advance: bool = True) -> None:
        """
        毎フレーム呼ぶ。

        allow_advance が偽の間は自動送りを止める。復帰直後でパネルがまだ映って
        いない時間帯に写真を送ってしまわないようにするため（display_manager.is_ready）。
        """
        # main.py のループは 入力処理 -> update の順なので、ここで戻すと
        # 1フレームに届いた2回目以降の手動送りだけが落ちる
        self._manual_advanced = False

        self._recreate_if_needed()
        self._collect_preloaded()

        # フェード中もここで呼ぶ。_last_change が古いままなので比が 1.0 を超え、
        # クランプされて「満タンのまま維持」になる（ユーザー選択の挙動）
        self._overlay.set_progress(self._progress(now))

        if self._fading:
            self._advance_fade(now)
            return

        if not allow_advance or not self._photos:
            return

        interval = float(self._config.get('interval', 10))
        if now - self._last_change >= interval:
            self.request_next(auto=True)

    def _progress(self, now: float) -> float | None:
        """
        次の自動送りまでの経過を 0.0〜1.0 で返す。

        写真が無ければ表示しようがないので None（カウントダウンゲージ非表示の合図）。
        `interval <= 0` は「即座に送る」設定であり満タン扱いにする。
        `_last_change` は自動送り・手動送り・履歴戻り・写真リスト差し替えの
        いずれでも更新済みのため、新しい状態は持たずそのまま流用する。
        """
        if not self._photos:
            return None
        interval = float(self._config.get('interval', 10))
        if interval <= 0:
            return 1.0
        ratio = (now - self._last_change) / interval
        return max(0.0, min(1.0, ratio))

    def request_next(self, auto: bool = False) -> None:
        """ 次の写真へ。用意できていなければ見送る（ブロックしない） """
        if not self._photos or self._fading:
            return
        if not auto:
            # 自動送りは interval で律速されているのでガードしない
            if self._manual_advanced:
                return
            self._manual_advanced = True
        if self._next_tex is None:
            if not auto:
                logger.info('次の写真がまだ用意できていません')
            # 自動送りで間に合わなかった場合は先読みを促してタイマーを引き直す
            self._last_change = time.monotonic()
            self._ensure_preload()
            return

        self._history.append(self._index)
        self._index = self._next_index()

        if auto:
            self._begin_fade()
        else:
            # 手動送りは瞬間表示。逆方向（request_prev）は先読みしていないため
            # 必ず瞬間表示になり、順方向だけフェードすると挙動が非対称になる。
            # 先読みを2枚に増やして逆方向もフェードさせる案は、
            # 「先読みを次の1枚より増やさない」（禁止パターン）に抵触するため採らない。
            self._update_overlay()
            self._swap_to_next(time.monotonic())

    def request_prev(self) -> None:
        """ 履歴を1つ戻る。履歴が空なら何もしない（先頭より前には戻れない） """
        if not self._photos or self._fading or not self._history:
            return
        if self._manual_advanced:
            return
        self._manual_advanced = True
        self._index = self._history.pop()
        self._drop_next()
        self._request_load(self._index, as_current=True)
        self._last_change = time.monotonic()

    def on_enter(self) -> None:
        """ 画面契約を満たすためのフック。スライドショーは常駐画面なので何もしない """
        pass

    def on_leave(self) -> None:
        pass

    def handle_input(self, kind: str, x: int, y: int) -> str | None:
        """
        画面共通契約に沿った入力処理。スライドショーはタップの離しか反応しない
        （ドラッグやスライダー操作を持たないため DOWN / MOVE は無視する）。
        """
        if kind != TAP_UP:
            return None
        return self.handle_tap(x, y)

    def handle_tap(self, x: int, y: int) -> str | None:
        """
        タップを処理する。メニューを開くべきときだけ ACTION_MENU を返す。

        判定順序が重要。歯車が表示中でそこを押したならメニューだけを処理し、
        写真送りの判定はしない。
        """
        if self._overlay.gear_hit(x, y):
            return ACTION_MENU

        width = self._r.size[0]
        if x < width * ZONE_PREV_RATIO:
            self.request_prev()
            self.notify_activity()
        elif x > width * ZONE_NEXT_RATIO:
            self.request_next()
            self.notify_activity()
        else:
            self._overlay.show_transient()
        return None

    # -------------------------------------------------------------------- 描画

    def draw(self) -> None:
        if self._fading:
            # 遷移の描画は transitions.py に委譲する。current を alpha=255 で
            # 描くかどうかも含めて遷移ごとに異なる（例: wipe は current の上に
            # 黒帯を重ねる）ため、ここでは current を先に描かない。
            transitions.draw(self._transition, self._r, self._dst_rect,
                              self._current_tex, self._next_tex, self._fade_progress())
            return
        if self._current_tex is not None:
            self._draw_texture(self._current_tex, 255)

    def _dst_rect(self, texture) -> pg.Rect:
        """
        等倍・中央配置の描画先矩形を返す。

        photo_cache が 1024x600 に収まるようリサイズ済みの画像を返すため、
        ここでは拡縮しない（禁止パターン: 描画パスでのリサイズ）。

        **配置の式はこの1か所に集約する。** 通常時の描画（_draw_texture）と
        各遷移（`transitions.py`）で別々に計算すると必ずずれる。
        `transitions.py` の描画関数へそのまま渡せるよう、引数はテクスチャ1つだけにしてある。
        """
        width, height = self._r.size
        tw, th = texture.width, texture.height
        return pg.Rect((width - tw) // 2, (height - th) // 2, tw, th)

    def _draw_texture(self, texture, alpha: int) -> None:
        """ 等倍・中央配置で描く """
        texture.alpha = alpha
        texture.draw(dstrect=self._dst_rect(texture))

    # ------------------------------------------------------------------ 遷移

    def _begin_fade(self) -> None:
        """
        フェードを開始する。使う遷移の種類はここで1回だけ決め、フェード中は固定する
        （`transition` の設定を毎フレーム読み直すと、フェードの途中で描画方式が
        切り替わって破綻する）。
        """
        self._fading = True
        self._fade_start = time.monotonic()
        self._next_tex.blend_mode = pg.BLENDMODE_BLEND
        raw = self._config.get('transition', transitions.CROSSFADE)
        self._transition = transitions.resolve_transition(
            raw, self._last_transition_kind, self._transition_warned)
        self._last_transition_kind = self._transition
        # 自動送り（interval 秒に1回）でしか呼ばれないため毎フレームログにはならない
        logger.info('遷移を開始: %s', self._transition)
        self._update_overlay()

    def _fade_progress(self) -> float:
        """ フェード開始からの経過を 0.0〜1.0 にクランプして返す """
        duration = max(0.05, float(self._config.get('transition_duration', 1.0)))
        ratio = (time.monotonic() - self._fade_start) / duration
        return max(0.0, min(1.0, ratio))

    def _advance_fade(self, now: float) -> None:
        duration = max(0.05, float(self._config.get('transition_duration', 1.0)))
        if now - self._fade_start < duration:
            return
        self._swap_to_next(now)

    def _swap_to_next(self, now: float) -> None:
        """ 次の写真を現在の写真にする。古いテクスチャは参照を落として解放させる """
        self._current_tex = self._next_tex
        self._current_path = self._next_path
        self._next_tex = None
        self._next_path = None
        self._fading = False
        self._last_change = now
        self._ensure_preload()

    # ------------------------------------------------------------------ 先読み

    def _next_index(self) -> int:
        return (self._index + 1) % len(self._photos)

    def _ensure_preload(self) -> None:
        """ 次の1枚だけを先読みする。2枚以上は先読みしない（禁止パターン） """
        if not self._photos or self._next_tex is not None:
            return
        target = self._next_index()
        if self._preloading_index == target:
            return
        self._request_load(target, as_current=False)

    def _request_load(self, index: int, as_current: bool) -> None:
        """ ワーカースレッドで画像を用意させる。SDL には触らせない """
        if not (0 <= index < len(self._photos)):
            return
        asset_id = self._photos[index]['id']
        self._preloading_index = index

        def worker() -> None:
            path = self._source.ensure_photo(asset_id)
            if self._stop_event.is_set():
                return
            self._preload_queue.put((index, as_current, path))

        thread = threading.Thread(target=worker, name=f'preload-{index}', daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()

    def _collect_preloaded(self) -> None:
        """
        ワーカーの結果を取り込んでテクスチャ化する。

        **テクスチャ生成は必ずメインスレッドで行う。** SDL の資源をスレッドから
        触らないための境界がここ。
        """
        while True:
            try:
                index, as_current, path = self._preload_queue.get_nowait()
            except queue.Empty:
                return

            if path is None:
                logger.warning('写真を用意できませんでした: index=%d', index)
                if as_current:
                    # 1枚落ちてもスライドショーは続ける
                    self._index = (index + 1) % len(self._photos) if self._photos else 0
                    self._request_load(self._index, as_current=True)
                continue

            texture = self._r.texture_from_image(path)
            if texture is None:
                continue

            if as_current:
                self._current_tex = texture
                self._current_path = path
                self._index = index
                self._last_change = time.monotonic()
                self._update_overlay()
                self._ensure_preload()
            else:
                self._next_tex = texture
                self._next_path = path

    def _drain_queue(self) -> None:
        while True:
            try:
                self._preload_queue.get_nowait()
            except queue.Empty:
                return

    # ------------------------------------------------------- SDL 再生成への追従

    def _recreate_if_needed(self) -> None:
        """
        消灯からの復帰で SDL を作り直すと、テクスチャはすべて無効になる。
        保持しているパスから作り直す。これを怠ると復帰後に真っ黒になる。
        """
        if self._gen == self._r.generation:
            return
        first_time = self._gen < 0
        self._gen = self._r.generation
        if first_time:
            # 起動時の初期化。作り直すテクスチャはまだ無い
            return

        if self._current_path is not None:
            self._current_tex = self._r.texture_from_image(self._current_path)
        if self._next_path is not None:
            self._next_tex = self._r.texture_from_image(self._next_path)
            if self._next_tex is not None:
                self._next_tex.blend_mode = pg.BLENDMODE_BLEND
        logger.info('SDL の再生成にあわせてテクスチャを作り直しました（gen=%d）', self._gen)

    def _drop_textures(self) -> None:
        self._current_tex = None
        self._next_tex = None

    def _drop_next(self) -> None:
        self._next_tex = None
        self._next_path = None

    def _update_overlay(self) -> None:
        info = self._photos[self._index] if self._photos else None
        self._overlay.set_photo(self._index, len(self._photos), info)
        if info:
            # 実機ではコンソールログが唯一の調査手段になる。切り替えは残す
            # （interval 秒に1回なので毎フレーム出力にはならない）
            logger.info('写真を表示: %d/%d id=...%s',
                        self._index + 1, len(self._photos), str(info.get('id'))[-6:])
