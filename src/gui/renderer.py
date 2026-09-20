import logging
import os
from pathlib import Path

import pygame as pg
import pygame._sdl2.video as sdl2

logger = logging.getLogger(__name__)

# display_manager.py / motion_sensor.py と同じ式。呼び出し側（main.py）が
# IS_DEV_ENVIRONMENT でガードしてから wake_window 系を呼ぶ契約だが、
# 実機で誤って呼ばれた場合に pg.display を開いて DRM master を握ってしまうと
# DisplayManager の drmSetMaster と衝突するため、このモジュール自身でも防ぐ。
IS_DEV_ENVIRONMENT = os.environ.get('IS_DEV_ENVIRONMENT', 'false').lower() == 'true'

# Dockerfile の fonts-noto-cjk で入る。実機のイメージ内で実在を確認済み。
DEFAULT_FONT_PATH = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'

# タップイベントの種別
TAP_DOWN = 'down'
TAP_UP = 'up'
TAP_MOVE = 'move'

# UI レイアウトの基準解像度。widgets.py / gui/screens/ / overlay.py の寸法定数は
# すべてこの解像度における「論理px」として定義されており、実際の描画直前に
# Renderer.px() を通して「物理px」へ変換する（1024x600 では倍率が 1.0 になり
# 恒等変換になる。実機の回帰確認の前提。.claude/plans/peaceful-giggling-parrot.md）。
BASE_WIDTH = 1024
BASE_HEIGHT = 600


class Renderer:
    """
    SDL の Window / Renderer と、そこにぶら下がる資源を一手に引き受けるクラス

    **消灯時に SDL を破棄して DRM master を解放する設計のため、このクラスは
    生成と破棄を繰り返される。** 破棄すると生成済みのテクスチャはすべて無効になるので、
    テクスチャを保持する側は `generation` の変化を見て作り直さなければならない。
    これを怠ると復帰後に画面が真っ黒になる。

    `display_manager.DisplayManager` の on_release_display / on_recreate_display には
    このクラスの destroy / create を渡す。

    **論理px と物理px の契約**: `size` が確定すると同時に `ui_scale`
    （`max(1.0, min(width / BASE_WIDTH, height / BASE_HEIGHT))`）が一度だけ算出される。
    `widgets.py` / `gui/screens/` / `gui/overlay.py` のレイアウト定数は「基準解像度
    1024x600 における論理px」として定義されており、`Widget.rect` を組み立てる時点で
    `px()` を通して物理pxへ変換する契約になっている。**`font()` はこの変換を行わない
    （物理pxを受け取る契約のまま）**。オーバーレイの文字サイズ（`comment_font_size`
    由来）はスケール対象外のため、ここで一律に変換すると対象外の値にも掛かってしまう。
    `Label`/`Button` の `font_size` は論理pxで受け取り、`draw()` の中で `px()` してから
    `font()`/`Text.set()` へ渡す（呼び出し側の二重適用を避けるための取り決め）。
    """

    def __init__(self, font_path: str = DEFAULT_FONT_PATH,
                 title: str = 'pi-photo-frame') -> None:
        self.font_path = font_path
        self._title = title
        self._window: sdl2.Window | None = None
        self._renderer: sdl2.Renderer | None = None
        self._size = (0, 0)
        # size と同じ寿命（create() で算出し、destroy() では触らない）。
        # size が未確定 (0, 0) の間は 1.0 を返す
        self._ui_scale = 1.0
        # SDL を破棄するたびに増やす。テクスチャの作り直し判定に使う
        self._generation = 0
        # 開発環境（Dev Container / x11）の消灯中だけ使う、クリック待ちの黒い
        # ウィンドウ。実機では一度も使われない（呼び出し側とこのモジュールの
        # 両方で IS_DEV_ENVIRONMENT を判定するため）。Window/Renderer とは別の pg.display
        # コンテキストなので、alive や generation には一切影響しない
        self._wake_surface: pg.Surface | None = None
        self._fonts: dict[int, pg.font.Font] = {}
        # 実機でどの入力イベント種別が実際に発生するかを確かめるための観測用集合。
        # FINGERMOTION が実機のタッチパネルで発生するかは未検証（Slider のドラッグ /
        # ScrollView のスクロールに関わる）。create() のたびにリセットする
        # （destroy/create のたびにリセットする理由: 消灯復帰のたびに SDL が
        # タッチデバイスを udev 経由で再列挙するため、復帰後も同じ種別が
        # 引き続き発生するかを毎回確認できるようにするため。プロセス通じて1回だと
        # 復帰後に発生しなくなった場合を見逃す）。1種別あたり create() ごとに
        # 最大1回しかログを出さないため「復帰のたびに大量に出る」ことはない。
        self._seen_event_types: set[int] = set()

    # ------------------------------------------------------------ ライフサイクル

    @property
    def alive(self) -> bool:
        return self._renderer is not None

    @property
    def generation(self) -> int:
        """ SDL の世代。破棄のたびに増える。テクスチャの有効性はこれで判断する """
        return self._generation

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    @property
    def ui_scale(self) -> float:
        """
        1024x600 を基準にした UI の拡大率。下限は 1.0（縮小はしない）。

        `size` と同じ寿命で `create()` の中で一度だけ算出する。以降は
        `destroy()` するまで固定値として使い回す（毎フレーム計算する理由がない）。
        """
        return self._ui_scale

    def px(self, value: int) -> int:
        """
        論理px（基準解像度 1024x600 における値）を物理pxへ変換する。

        **丸めは `int()` に統一する。** `ui_scale == 1.0` のとき
        `int(v * 1.0) == v` で恒等変換になることが、1024x600 での現行版との
        完全一致（実機の回帰確認の前提）を支える。`round()` と混在させないこと。
        """
        return int(value * self._ui_scale)

    def create(self) -> None:
        """ Window と Renderer を生成する。既に生きていれば何もしない """
        # 復帰待ちウィンドウを開いたまま pg.display.init() を呼ぶと
        # 同じ pg.display コンテキストの二重初期化になるため、必ず先に閉じる
        # （開発環境の消灯復帰経路。実機ではウィンドウが開くことがないため実質no-op）
        self.close_wake_window()

        if self.alive:
            return

        pg.display.init()
        if not pg.font.get_init():
            pg.font.init()

        info = pg.display.Info()
        width, height = info.current_w, info.current_h
        self._window = sdl2.Window(self._title, size=(width, height),
                                   fullscreen_desktop=True)
        # vsync=True でページフリップに同期させる。fps が垂直同期(49.61Hz)付近に
        # 収まることが「実際に表示されている」根拠になる（9-4 / 9-10 の教訓）。
        self._renderer = sdl2.Renderer(self._window, vsync=True)
        self._size = (width, height)
        # 下限 1.0 でクランプ（1024x600 未満への縮小は対象外）。size 確定直後の
        # この1回だけで算出し、以降は size と同じ寿命として使い回す
        self._ui_scale = max(1.0, min(width / BASE_WIDTH, height / BASE_HEIGHT))
        pg.mouse.set_visible(False)
        # 新しい世代では入力イベント種別の観測をやり直す（上記コメント参照）
        self._seen_event_types.clear()

        logger.info(
            '描画を生成しました: driver=%s size=%dx%d render_driver=%s gen=%d ui_scale=%.2f',
            pg.display.get_driver(), width, height,
            os.environ.get('SDL_RENDER_DRIVER', '(未設定)'), self._generation, self._ui_scale)

    def destroy(self) -> None:
        """
        Window と Renderer を破棄して DRM master を解放する。

        フォントは Surface を作るだけで SDL の資源を持たないため保持したままでよい。
        テクスチャは無効になるので generation を進めて利用側へ知らせる。
        """
        # 消灯中に終了する経路では alive が既に False（消灯時にここへ来て
        # SDL は破棄済み）でも復帰待ちウィンドウだけは開いたままのことがある。
        # alive の判定より前に必ず閉じる（create() と対にした処理）
        self.close_wake_window()

        if not self.alive:
            return

        # ここで黒く塗っても意味がない。塗れるのは SDL 自身のフレームバッファで、
        # 破棄と同時に解放され、CRTC は /dev/fb0（fbcon 用）を指し直すためである。
        # 復帰時にコンソールの文字が見える問題は、ホスト側で fbcon を unbind し
        # fb0 をゼロクリアして解決した（tools/host-setup/ を参照）。
        self._renderer = None
        self._window = None
        pg.display.quit()
        self._generation += 1
        logger.info('描画を破棄しました（DRM master を解放 / gen=%d）', self._generation)

    # ---------------------------------------------------- 開発環境の復帰待ち窓

    def open_wake_window(self) -> None:
        """
        開発環境の消灯中に、クリック待ちの黒いウィンドウを開く。

        実機は消灯中も DRM master を握ったまま /dev/input と GPIO を直読みできるが、
        Dev Container（x11）は destroy() で SDL ごとウィンドウが消えるため、
        noVNC のクリックを拾う手段がそもそも無くなる（/dev/input も人感センサーの
        モックも使えない）。実機で確立した「破棄して master を解放する」設計は
        崩さず、開発環境のときだけ最低限の受け皿を用意して復帰の入口を確保する。

        Renderer / Texture はここでは作らない。生成すると destroy() 済みの
        SDL コンテキストの外で GPU 資源を持つことになり世代管理と噛み合わないうえ、
        1024x600 全面を毎フレーム描き直す口実にもなりかねない
        （禁止パターン: フルスクリーンの CPU 側合成 / 毎フレーム再描画）。
        黒で塗って flip するのは開いた瞬間の1回だけで、以後は何も描き直さない。
        """
        if not IS_DEV_ENVIRONMENT:
            # 実機で呼ばれると pg.display.init() が DRM master を握り、
            # DisplayManager 側の drmSetMaster と衝突する（DRM master は1プロセスのみ）。
            # 呼び出し側（main.py）のガードに加え、ここでも必ず弾く。
            logger.error('open_wake_window() は開発環境専用です（IS_DEV_ENVIRONMENT=false）')
            return
        if self._wake_surface is not None:
            return
        pg.display.init()
        info = pg.display.Info()
        width, height = info.current_w, info.current_h
        self._wake_surface = pg.display.set_mode((width, height), pg.FULLSCREEN)
        pg.mouse.set_visible(False)
        self._wake_surface.fill((0, 0, 0))
        pg.display.flip()
        logger.info('復帰待ちウィンドウを開きました（開発環境）: %dx%d', width, height)

    def close_wake_window(self) -> None:
        """ 復帰待ちウィンドウを閉じる。SDL の Window/Renderer を作り直す前に必ず呼ぶこと """
        if self._wake_surface is None:
            return
        self._wake_surface = None
        pg.display.quit()
        logger.info('復帰待ちウィンドウを閉じました（開発環境）')

    def poll_wake_window(self) -> tuple[bool, bool]:
        """
        復帰待ちウィンドウの入力を見る。開いていなければ遅延で開いてから見る。

        戻り値は (復帰要求があったか, 終了要求があったか)。クリック・タップ・
        キー入力のいずれかで復帰要求とする。QUIT は呼び出し側が `_should_stop` 等の
        終了フラグへ反映できるよう、復帰要求とは別に返す。

        実機（IS_DEV_ENVIRONMENT=false）では open_wake_window() を呼ばず、
        pg.event.get() も呼ばない。消灯中の実機は SDL が破棄済みで
        pg.display が未初期化のため、呼ぶと「video system not initialized」で落ちる。
        """
        if not IS_DEV_ENVIRONMENT:
            return False, False
        self.open_wake_window()
        wake = False
        quit_requested = False
        for event in pg.event.get():
            if event.type == pg.QUIT:
                quit_requested = True
            elif event.type in (pg.MOUSEBUTTONDOWN, pg.FINGERDOWN, pg.KEYDOWN):
                wake = True
        return wake, quit_requested

    # -------------------------------------------------------------------- 描画

    def clear(self, color: tuple[int, int, int] = (0, 0, 0)) -> None:
        if not self.alive:
            return
        self._renderer.draw_color = (*color, 255)
        self._renderer.clear()

    def present(self) -> None:
        if not self.alive:
            return
        self._renderer.present()

    def fill_rect(self, rect: pg.Rect, color: tuple[int, int, int], alpha: int = 255) -> None:
        """ 単色の矩形を塗る。オーバーレイの帯やボタンの背景に使う """
        if not self.alive:
            return
        self._renderer.draw_blend_mode = pg.BLENDMODE_BLEND
        self._renderer.draw_color = (*color, alpha)
        self._renderer.fill_rect(rect)

    def draw_rect(self, rect: pg.Rect, color: tuple[int, int, int],
                  alpha: int = 255, width: int = 1) -> None:
        """
        矩形の輪郭だけを描く。ボタンの枠やアルバム選択の選択枠に使う。

        `Renderer.draw_rect` は線幅の指定を持たないため、`width` 分だけ矩形を
        内側へ縮めながら重ね塗りして太さを表現する。太い枠でも塗るのは
        輪郭線だけで、CPU 側でのフルスクリーン合成にはならない。
        """
        if not self.alive:
            return
        self._renderer.draw_blend_mode = pg.BLENDMODE_BLEND
        self._renderer.draw_color = (*color, alpha)
        for i in range(max(1, width)):
            r = pg.Rect(rect.x + i, rect.y + i, rect.width - i * 2, rect.height - i * 2)
            if r.width <= 0 or r.height <= 0:
                break
            self._renderer.draw_rect(r)

    # ---------------------------------------------------------------- テクスチャ

    def texture_from_surface(self, surface: pg.Surface) -> sdl2.Texture | None:
        """
        消灯中は SDL が無いので None を返す。

        消灯と描画の境目でこのメソッドが呼ばれることがあり、素通しすると
        「Parameter 'renderer' is invalid」で落ちる。
        """
        if not self.alive:
            return None
        return sdl2.Texture.from_surface(self._renderer, surface)

    def texture_from_image(self, path: str | Path) -> sdl2.Texture | None:
        """
        画像ファイルからテクスチャを作る。

        **ここでリサイズはしない。** 表示解像度への変換は photo_cache が
        キャッシュ生成時に済ませている（禁止パターン: 描画パスでのリサイズ）。
        """
        if not self.alive:
            return None
        try:
            surface = pg.image.load(str(path))
        except pg.error as e:
            logger.warning('画像を読み込めませんでした: %s (%s)', path, e)
            return None
        try:
            return self.texture_from_surface(surface)
        finally:
            del surface

    # -------------------------------------------------------------------- 文字

    def font(self, size: int) -> pg.font.Font:
        """ サイズ別にフォントをキャッシュする（読み込みは 1 種あたり数十 ms かかる） """
        cached = self._fonts.get(size)
        if cached is None:
            cached = pg.font.Font(self.font_path, size)
            self._fonts[size] = cached
        return cached

    def text_texture(self, text: str, size: int,
                     color: tuple[int, int, int] = (255, 255, 255)
                     ) -> tuple[sdl2.Texture, int, int] | None:
        """
        文字列からテクスチャを作り、(texture, width, height) を返す。

        **毎フレーム呼ばないこと。** 内容が変化したときだけ呼び、以降は
        できたテクスチャを使い回す（禁止パターン: オーバーレイの毎フレーム再描画）。
        """
        if not text or not self.alive:
            return None
        surface = self.font(size).render(text, True, color)
        texture = self.texture_from_surface(surface)
        if texture is None:
            return None
        return texture, surface.get_width(), surface.get_height()

    # -------------------------------------------------------------------- 入力

    def normalize_events(self, events: list[pg.event.Event]) -> list[tuple[str, int, int]]:
        """
        タッチとマウスを同じ形（種別, x, y）に揃えて返す。

        実機はタッチ（FINGERDOWN/UP/MOTION、座標は 0.0〜1.0 の正規化値）、
        Dev Container はマウス（MOUSEBUTTON*/MOTION、ピクセル座標）でイベントが異なる。
        ここで吸収して、画面側は座標だけを見ればよいようにする。

        MOTION は TAP_MOVE として返す。マウスは左ボタンを押したままの移動だけを
        対象にする（ボタンを押していない MOUSEMOTION は毎フレーム大量に来るため、
        素通しするとウィジェット側が無駄な当たり判定を繰り返すことになる）。
        指のドラッグかどうかは実機で FINGERMOTION が実際に発生するかによるため、
        発生しない場合は呼び出し側が DOWN/UP の座標差分で代替する
        （SPECIFICATION.md 「実装時に確かめること」参照。未検証）。
        """
        if not self.alive:
            return []
        width, height = self._size
        taps: list[tuple[str, int, int]] = []
        for event in events:
            if event.type == pg.FINGERDOWN:
                x, y = int(event.x * width), int(event.y * height)
                self._log_first_seen(event.type, 'FINGERDOWN', TAP_DOWN, x, y)
                taps.append((TAP_DOWN, x, y))
            elif event.type == pg.FINGERUP:
                x, y = int(event.x * width), int(event.y * height)
                self._log_first_seen(event.type, 'FINGERUP', TAP_UP, x, y)
                taps.append((TAP_UP, x, y))
            elif event.type == pg.FINGERMOTION:
                x, y = int(event.x * width), int(event.y * height)
                self._log_first_seen(event.type, 'FINGERMOTION', TAP_MOVE, x, y)
                taps.append((TAP_MOVE, x, y))
            elif event.type == pg.MOUSEBUTTONDOWN and event.button == 1:
                x, y = event.pos[0], event.pos[1]
                self._log_first_seen(event.type, 'MOUSEBUTTONDOWN', TAP_DOWN, x, y)
                taps.append((TAP_DOWN, x, y))
            elif event.type == pg.MOUSEBUTTONUP and event.button == 1:
                x, y = event.pos[0], event.pos[1]
                self._log_first_seen(event.type, 'MOUSEBUTTONUP', TAP_UP, x, y)
                taps.append((TAP_UP, x, y))
            elif event.type == pg.MOUSEMOTION and event.buttons[0]:
                x, y = event.pos[0], event.pos[1]
                self._log_first_seen(event.type, 'MOUSEMOTION', TAP_MOVE, x, y)
                taps.append((TAP_MOVE, x, y))
        return taps

    def _log_first_seen(self, event_type: int, type_name: str, tap_kind: str,
                        x: int, y: int) -> None:
        """
        イベント種別ごとに初回の1回だけ INFO ログを出す。

        実機で FINGERMOTION が実際に発生するかなど、種別の発生有無そのものが
        未検証事項（SPECIFICATION.md「実装時に確かめること」）のため、恒久的な
        観測点として残す。`set` への追加可否で判定するのみで、毎イベントの
        重い処理（フォーマットや I/O）は「初めて見た種別」のときにしか行わない。
        """
        if event_type in self._seen_event_types:
            return
        self._seen_event_types.add(event_type)
        logger.info('初めて受信した入力イベント: %s -> %s (%d, %d)',
                    type_name, tap_kind, x, y)
