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

# 次の写真が未用意の間、カウントダウンゲージを止める位置（0.0〜1.0）。
# 右端の手前で止めて「もうすぐ送る」ではなく「次の写真を待っている」ことを示す。
# 0 に戻してループさせると、送りが近いのか遠いのか分からなくなるため
PROGRESS_HOLD_RATIO = 0.95


class SlideshowScreen:
    """
    写真を切り替えて表示する画面

    **常駐する写真テクスチャは現在と次の最大2枚。** 先読みも次の1枚のみで、
    どちらも .claude/architecture.md の禁止パターンに直接対応する。

    photo-frame は次の画像を `Event.wait(5)` で待っており、間に合わないと
    メインスレッドが最大5秒固まっていた。ここでは待たず、用意できていなければ
    自動送りを保留する（ゲージは PROGRESS_HOLD_RATIO で止め、タイマーは引き直さない）。
    読み込みが届いた次のフレームで即座に送る。
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
        # _next_tex が実際に持っている写真の index。_next_candidate とは別に持つ。
        # reload_current() 中に先読みの失敗で候補が進み、その後 as_current の結果が
        # 届いて候補が _index + 1 に戻ると、候補と next_tex の中身がずれる。
        # 送りは候補ではなくこの値へ進めて、表示とカウンタを必ず一致させる
        self._next_loaded_index: int | None = None
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
        # 「次に表示する写真」の index。None のときは _index + 1。
        # 先読みが失敗（取得不能・画素上限超過など）した写真を飛ばして先へ進めるために
        # 持つ。固定の _index + 1 のままだと、失敗した index を _preloading_index が
        # 指し続けて先読みも送りも二度と進まず、永久に同じ写真で止まる
        self._next_candidate: int | None = None
        # 候補が一周して現在の写真に戻った（次の写真が1枚も用意できない）状態。
        # 立っている間は先読みを起こさない（ワーカーの空回りを避ける）。
        # 次の送りのタイミング（request_next）で下ろして最初からやり直す
        self._preload_exhausted = False
        # 自動送りが次の写真の未用意で保留されているとき、全部失敗で止まった先読みを
        # やり直す次の時刻（monotonic）。毎フレーム restart しないための間隔制御
        self._preload_retry_at = 0.0
        # 「待っています」ログを待ち始めの1回に絞るための印
        self._waiting_logged = False
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
        self._reset_next_candidate()
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
        # クランプされて「満タンのまま維持」になる（ユーザー選択の挙動）。
        # 次の写真が未用意でフェード中でないときは PROGRESS_HOLD_RATIO で止まる
        self._overlay.set_progress(self._progress(now))

        if self._fading:
            self._advance_fade(now)
            return

        if not allow_advance or not self._photos:
            return

        interval = float(self._config.get('interval', 10))
        if now - self._last_change >= interval:
            if self._next_tex is not None or now >= self._preload_retry_at:
                self.request_next(auto=True)
        else:
            self._waiting_logged = False

    def _progress(self, now: float) -> float | None:
        """
        次の自動送りまでの経過を 0.0〜1.0 で返す。

        写真が無ければ表示しようがないので None（カウントダウンゲージ非表示の合図）。
        `interval <= 0` は「即座に送る」設定であり満タン扱いにする。
        フェード中でなく次の写真が未用意のときは PROGRESS_HOLD_RATIO で頭打ちにする
        （送りを保留して待っている状態の表示。フェード中は従来どおり満タン維持）。
        `_last_change` は自動送り・手動送り・履歴戻り・写真リスト差し替えの
        いずれでも更新済みのため、新しい状態は持たずそのまま流用する。
        """
        if not self._photos:
            return None
        interval = float(self._config.get('interval', 10))
        if interval <= 0:
            return 1.0
        ratio = (now - self._last_change) / interval
        ratio = max(0.0, min(1.0, ratio))
        if not self._fading and self._next_tex is None:
            ratio = min(ratio, PROGRESS_HOLD_RATIO)
        return ratio

    def request_next(self, auto: bool = False) -> None:
        """
        次の写真へ。用意できていなければ送らない（ブロックしない）。

        自動送りは _last_change を触らず保留する（ゲージは止まり、届いたら即送る）。
        手動送りは従来どおりタイマーを引き直す。
        """
        if not self._photos or self._fading:
            return
        if not auto:
            # 自動送りは interval で律速されているのでガードしない
            if self._manual_advanced:
                return
            self._manual_advanced = True
        if self._next_tex is None:
            now = time.monotonic()
            if auto:
                # タイマーは戻さない。全部失敗で止まった先読みのやり直し
                # （Wi-Fi 断からの復旧手段）は interval 間隔で続ける
                if not self._waiting_logged:
                    self._waiting_logged = True
                    logger.info('次の写真の読み込みを待っています')
                self._preload_retry_at = now + float(self._config.get('interval', 10))
            else:
                logger.info('次の写真がまだ用意できていません')
                self._last_change = now
            self._ensure_preload(restart=True)
            return
        self._waiting_logged = False

        self._history.append(self._index)
        self._index = (self._next_loaded_index if self._next_loaded_index is not None
                       else self._next_index())
        self._reset_next_candidate()

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
        self._next_loaded_index = None
        self._fading = False
        self._last_change = now
        self._ensure_preload()

    # ------------------------------------------------------------------ 先読み

    def _next_index(self) -> int:
        if self._next_candidate is not None:
            return self._next_candidate
        return (self._index + 1) % len(self._photos)

    def _reset_next_candidate(self) -> None:
        """ 候補を _index + 1 へ戻す。_index や次のテクスチャを捨てる操作のたびに呼ぶ """
        self._next_candidate = None
        self._preload_exhausted = False

    def _ensure_preload(self, restart: bool = False) -> None:
        """
        次の1枚だけを先読みする。2枚以上は先読みしない（禁止パターン）。

        restart は送りのタイミング（request_next）だけが真にする。全部失敗して
        止まっている状態（_preload_exhausted）は、そこで初めて最初から試し直す。
        """
        if not self._photos or self._next_tex is not None:
            return
        if self._preload_exhausted:
            if not restart:
                return
            self._reset_next_candidate()
        target = self._next_index()
        if self._preloading_index == target:
            return
        self._request_load(target, as_current=False)

    def _request_load(self, index: int, as_current: bool) -> None:
        """ ワーカースレッドで画像を用意させる。SDL には触らせない """
        if not (0 <= index < len(self._photos)):
            return
        asset_id = self._photos[index]['id']
        # ワーカーから self._photos を触らない（set_photos() で差し替わりうる）ため、
        # 日付の有無はここで決めておく
        needs_date = not self._photos[index].get('date')
        self._preloading_index = index

        def worker() -> None:
            try:
                path = self._source.ensure_photo(asset_id)
            except Exception:
                # 例外でスレッドが死ぬと _preload_queue に何も積まれず、
                # _preloading_index がこの index に固定されたまま先読みが
                # 二度と進まなくなる。既存の「取得失敗（path=None）」と
                # 同じ経路に合流させ、_collect_preloaded() の既存の
                # 復旧処理（as_current の場合は次の写真へ進める）に任せる
                logger.exception('写真の先読み中に例外が発生しました: index=%d', index)
                path = None
            # 写真リストに日付が無いときだけ、キャッシュに残した撮影日を引く。
            # ファイル読みなのでメインスレッドではなくここ（ワーカー）で行う
            date = ''
            if path is not None and needs_date:
                try:
                    date = self._source.cached_date(asset_id)
                except Exception:
                    logger.exception('撮影日の取得中に例外が発生しました: index=%d', index)
            if self._stop_event.is_set():
                return
            self._preload_queue.put((index, as_current, path, asset_id, date))

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
                index, as_current, path, asset_id, date = self._preload_queue.get_nowait()
            except queue.Empty:
                return

            if not (0 <= index < len(self._photos)
                    and self._photos[index]['id'] == asset_id):
                # set_photos() で写真リストが差し替わったあとに、旧リストの
                # ワーカーから遅れて届いた結果。index が偶然一致しても別の写真なので捨てる
                # （差し替え時に新しい as_current の読み込みは発行済み）
                logger.info('旧リストの先読み結果を捨てました: index=%d', index)
                continue

            if date and not self._photos[index].get('date'):
                # 取得元の一覧に撮影日が無く、キャッシュに残した値で補う。リスト JSON は
                # 書き換えず（次回もキャッシュから補える）、メモリ上の1件だけ差し替える
                self._photos[index] = {**self._photos[index], 'date': date}

            if not as_current:
                # 結果を取り込んだので、この index の先読みはもう飛んでいない。
                # 戻さないと _ensure_preload() が「同じ index を先読み中」と見て止まる
                if self._preloading_index == index:
                    self._preloading_index = None
                if index != self._next_index():
                    # 前へ戻るなどで候補が変わったあとに遅れて届いた古い先読み結果。
                    # 別の写真を「次」にしないよう捨てる
                    logger.info('古い先読み結果を捨てました: index=%d', index)
                    continue

            if path is None:
                logger.warning('写真を用意できませんでした: index=%d', index)
                if not as_current:
                    self._on_next_failed(index)
                else:
                    # 1枚落ちてもスライドショーは続ける
                    self._index = (index + 1) % len(self._photos)
                    self._reset_next_candidate()
                    self._request_load(self._index, as_current=True)
                continue

            texture = self._r.texture_from_image(path)
            if texture is None:
                if not as_current:
                    self._on_next_failed(index)
                continue

            if as_current:
                self._current_tex = texture
                self._current_path = path
                self._index = index
                self._reset_next_candidate()
                self._last_change = time.monotonic()
                self._update_overlay()
                self._ensure_preload()
            else:
                self._next_tex = texture
                self._next_path = path
                self._next_loaded_index = index

    def _on_next_failed(self, index: int) -> None:
        """
        「次の写真」の先読みが失敗したとき、その1枚を飛ばして先の写真を先読みし直す
        （as_current の失敗処理と同じ考え方）。

        届いた結果が現在の候補でなければ（前へ戻る等で古くなった）何もしない。
        一周して現在の写真へ戻ったら止める。ここで続けると全滅時にワーカーが
        空回りするため、次の送り（request_next）まで待つ。
        """
        if not self._photos or index != self._next_index():
            return
        following = (index + 1) % len(self._photos)
        self._preloading_index = None
        if following == self._index:
            self._next_candidate = None
            self._preload_exhausted = True
            logger.warning('次に表示できる写真が見つかりませんでした。次の送りで再試行します')
            return
        self._next_candidate = following
        self._request_load(following, as_current=False)

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
            else:
                # 作り直せなかった。パスを持ったまま先読み済み扱いにすると
                # 次の先読みが走らず止まるため、未取得へ戻して先読みし直させる
                self._next_path = None
                self._next_loaded_index = None
                self._preloading_index = None
                self._ensure_preload()
        logger.info('SDL の再生成にあわせてテクスチャを作り直しました（gen=%d）', self._gen)

    def _drop_textures(self) -> None:
        self._current_tex = None
        self._next_tex = None
        self._next_loaded_index = None

    def _drop_next(self) -> None:
        self._next_tex = None
        self._next_path = None
        self._next_loaded_index = None
        self._reset_next_candidate()

    def _update_overlay(self) -> None:
        info = self._photos[self._index] if self._photos else None
        self._overlay.set_photo(self._index, len(self._photos), info)
        if info:
            # 実機ではコンソールログが唯一の調査手段になる。切り替えは残す
            # （interval 秒に1回なので毎フレーム出力にはならない）
            logger.info('写真を表示: %d/%d id=...%s',
                        self._index + 1, len(self._photos), str(info.get('id'))[-6:])
