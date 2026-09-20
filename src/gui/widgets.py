"""
自作ウィジェット一式

基本設定画面・アルバム選択画面（次のステップ）から共通で使う部品を、
このステップでまとめて用意する。photo-frame の `widgets.py` はデッドコードだった
ため移植せず、ゼロから書き起こす。

**方針（.claude/architecture.md 厳守）**
- 枠・背景・つまみは `Renderer.fill_rect` / `Renderer.draw_rect` で GPU 描画する。
  1024x600 の Surface を CPU で組み立てて `Surface.blit` する案は、ドラッグ中に
  毎フレーム CPU 合成が走るため採らない。
- 文字だけ `Text`（`src/gui/text.py`）でテクスチャ化する。内容が変わらない限り
  作り直さない。`Text.set()` は不変なら早期リターンする契約なので、
  各ウィジェットの `draw()` 冒頭で毎回呼んでよい
  （`Renderer.generation` が変わったときの追従もこれで拾える）。
- タッチ前提のため当たり判定は最小 60px を確保する（`MIN_HIT_SIZE`）。

**論理px と物理px の契約（解像度スケーリング対応）**
- このモジュールの寸法定数（`MIN_HIT_SIZE` / `UI_*_FONT_SIZE` / `SCROLL_DRAG_THRESHOLD`
  等）は値を変えず、「基準解像度 1024x600 における論理px」として扱う。
- `Widget.rect` は**物理px**（当たり判定と `ScrollView` の座標計算が物理px基準のため）。
  呼び出し側（`gui/screens/` 各画面）が矩形を組み立てる時点で `Renderer.px()` を通す。
- `Label` / `Button` の `font_size` 引数は**論理pxで受け取る契約**。呼び出し側は
  定数をそのまま渡せばよく、`draw()` の内部で `px()` してから `Text.set()` へ渡す。
  **呼び出し側で先に `px()` すると二重適用になるので絶対にしないこと。**
- ウィジェット内部のマジックナンバー（`Toggle`/`Slider` の寸法、`Button`/`Spinner`
  の枠線太さ、`ScrollView.SCROLL_DRAG_THRESHOLD`）は各ウィジェットが `self._r.px()`
  を通してから使う。
"""

import logging
from typing import TYPE_CHECKING, Any

import pygame as pg

from src.gui.text import Text

if TYPE_CHECKING:
    from src.gui.renderer import Renderer

logger = logging.getLogger(__name__)

# タッチ操作の最小当たり判定サイズ
MIN_HIT_SIZE = 60

# コメント表示用の comment_font_size とは独立した UI 固定値
UI_FONT_SIZE = 28
UI_LABEL_FONT_SIZE = 20
UI_VALUE_FONT_SIZE = 22
# 基本設定画面の行見出し用。トグル/スピナー行は Label（既定 UI_FONT_SIZE=28px）、
# スライダー行は Slider.draw() 内の見出し（旧 UI_LABEL_FONT_SIZE=20px）で
# フォントサイズが不揃いだったため統一する。UI_LABEL_FONT_SIZE は
# album.py のアルバム名表示でも使っているため、値を変えずこちらを新設した。
UI_ROW_LABEL_FONT_SIZE = 24

# ScrollView: この移動量(px)を超えたらタップではなくドラッグと判定する
SCROLL_DRAG_THRESHOLD = 12

# 色（UI 全体で使い回す。ここもハードコードだが設定値ではなくレイアウト定数の扱い）
COLOR_PANEL = (50, 50, 50)
COLOR_PANEL_PRESSED = (80, 80, 80)
COLOR_BORDER = (200, 200, 200)
COLOR_TEXT = (255, 255, 255)
COLOR_ACCENT = (90, 160, 220)
COLOR_TRACK = (60, 60, 60)
COLOR_KNOB = (230, 230, 230)


class Widget:
    """
    ウィジェットの共通基底。

    `rect` はこのウィジェットが属する座標系での矩形。単体で使うときは画面座標、
    `ScrollView` に入れたときは content 座標（y=0 起点）になる
    （`ScrollView` が描画時・当たり判定時に画面座標へ変換する）。
    """

    # ScrollView が未決状態から行き先を決めるとき（移動量が閾値を超えた瞬間の1回だけ）、
    # 横方向の移動が縦方向以上ならこのウィジェットを掴む候補にする（True なのは Slider だけ）。
    # 「掴んだ瞬間に無条件でドラッグを占有する」という意味ではない点に注意
    # （以前の実装はそうなっており、スクロールしようとしてスライダーの行に触れただけで
    # 値が変わってしまう不具合の原因だった）。
    # Toggle / Spinner / Button は DOWN→UP の一瞬しか掴まれないため関係ない。
    captures_drag = False

    def __init__(self, rect: pg.Rect) -> None:
        self.rect = rect
        self.visible = True

    def handle_down(self, x: int, y: int) -> bool:
        """ 矩形内なら押下を受理して True を返す """
        return False

    def handle_move(self, x: int, y: int) -> None:
        pass

    def handle_up(self, x: int, y: int) -> Any:
        """ 戻り値はウィジェットごとに意味が違う（Button は bool、Slider/Spinner/Toggle は無し） """
        return None

    def handle_cancel(self) -> None:
        """
        ScrollView がこのウィジェットを「掴んでいた子」から手放すときに呼ぶ。

        スクロールへ切り替わったと判定された瞬間、それまで DOWN を受理していた子は
        UP を受け取れないまま終わる。押下ハイライトや仮の値をそのままにすると、
        指を離した後も操作中の見た目が残ってしまうため、ここで元に戻す契機を用意する。
        既定は no-op（Label など状態を持たないウィジェットは何もしなくてよい）。
        """
        pass

    def update(self, now: float) -> None:
        pass

    def draw(self) -> None:
        pass


class Label(Widget):
    """ 表示専用のテキスト。当たり判定は持たない """

    def __init__(self, renderer: 'Renderer', rect: pg.Rect, text: str,
                 font_size: int = UI_FONT_SIZE, color: tuple[int, int, int] = COLOR_TEXT,
                 centered: bool = False) -> None:
        super().__init__(rect)
        self._r = renderer
        self._text_str = text
        self._font_size = font_size
        self._color = color
        self._centered = centered
        self._text = Text(renderer)

    def set_text(self, text: str) -> None:
        self._text_str = text

    def draw(self) -> None:
        if not self.visible:
            return
        # font_size は論理pxで受け取る契約。ここで初めて物理pxへ変換する
        # （呼び出し側が px() 済みの値を渡すと二重適用になるため）。
        self._text.set(self._text_str, self._r.px(self._font_size), self._color)
        y = self.rect.y + (self.rect.height - self._text.height) // 2
        if self._centered:
            self._text.draw_centered(self.rect.centerx, y)
        else:
            self._text.draw(self.rect.x, y)


class Button(Widget):
    """
    押下中はハイライトし、UP が矩形内にあるときだけ発火する。

    `action` は呼び出し側（画面）が `handle_input` の戻り値として使う任意の値
    （`src/gui/screens/__init__.py` のアクション定数を想定）。
    """

    def __init__(self, renderer: 'Renderer', rect: pg.Rect, text: str,
                 action: str | None = None, font_size: int = UI_FONT_SIZE) -> None:
        super().__init__(rect)
        self._r = renderer
        self.action = action
        self._text_str = text
        self._font_size = font_size
        self._text = Text(renderer)
        self._pressed = False

    def set_text(self, text: str) -> None:
        self._text_str = text

    def handle_down(self, x: int, y: int) -> bool:
        if not self.visible or not self.rect.collidepoint(x, y):
            return False
        self._pressed = True
        return True

    def handle_up(self, x: int, y: int) -> bool:
        fired = self._pressed and self.visible and self.rect.collidepoint(x, y)
        self._pressed = False
        return fired

    def handle_cancel(self) -> None:
        # ScrollView がスクロールへ切り替わると、DOWN を受理した後でも
        # handle_up が呼ばれずに終わる。押下ハイライトを戻さないと、
        # 指を離してもボタンが押されたままの見た目が残ってしまう。
        self._pressed = False

    def draw(self) -> None:
        if not self.visible:
            return
        bg = COLOR_PANEL_PRESSED if self._pressed else COLOR_PANEL
        self._r.fill_rect(self.rect, bg, alpha=210)
        self._r.draw_rect(self.rect, COLOR_BORDER, alpha=200, width=self._r.px(2))
        # font_size は論理pxで受け取る契約（Label と同じ）
        self._text.set(self._text_str, self._r.px(self._font_size))
        self._text.draw_centered(self.rect.centerx,
                                 self.rect.y + (self.rect.height - self._text.height) // 2)


class Toggle(Widget):
    """ トラック＋つまみのスイッチ。タップで反転する """

    def __init__(self, renderer: 'Renderer', rect: pg.Rect, value: bool = False) -> None:
        # 当たり判定は rect そのまま（呼び出し側で MIN_HIT_SIZE を満たす大きさにする）
        super().__init__(rect)
        self._r = renderer
        self.value = value
        self.changed = False

    def handle_up(self, x: int, y: int) -> bool:
        if not self.visible or not self.rect.collidepoint(x, y):
            return False
        self.value = not self.value
        self.changed = True
        return True

    def draw(self) -> None:
        if not self.visible:
            return
        track_h = min(self.rect.height, self._r.px(32))
        track = pg.Rect(self.rect.x, self.rect.centery - track_h // 2,
                        self.rect.width, track_h)
        track_color = COLOR_ACCENT if self.value else COLOR_TRACK
        self._r.fill_rect(track, track_color, alpha=200)
        knob_d = track_h + self._r.px(8)
        knob_x = (self.rect.right - knob_d) if self.value else self.rect.x
        knob = pg.Rect(knob_x, self.rect.centery - knob_d // 2, knob_d, knob_d)
        self._r.fill_rect(knob, COLOR_KNOB, alpha=230)


class Slider(Widget):
    """
    DOWN で掴み、MOVE で追従、UP で確定する。

    DOWN の時点では値を書き換えない（ScrollView が未決状態から縦スクロールと
    判定した場合、掴んだ時点で値が動いていると `handle_cancel` で戻すまでの
    一瞬だけ見た目がずれるうえ、そもそも「触れただけで値が変わる」体験を避けたい
    ため）。値が動き始めるのは MOVE 以降で、UP で `_value_from_x` により確定する。
    トラックをタップしただけ（DOWN 直後に MOVE 無しで UP）の操作は、
    この UP 時点の確定で「タップした位置に値を置く」挙動として成立する。

    値は常に `step` の倍数へ丸める。呼び出し側（基本設定画面）は `changed` を見て
    ドラッグ確定（UP）のときだけ設定へ保存する（ドラッグ中の毎回保存は
    SD カードへの書き込みを増やすため避ける方針。ステップ2の担当）。
    """

    # ScrollView が未決状態から行き先を決めるとき、横方向の移動が縦方向以上なら
    # このウィジェットを掴む候補にする（Widget の captures_drag 参照）。
    # つまみを横に動かして値を変える操作を縦スクロールに奪われないための印であり、
    # 「掴んだ瞬間に無条件で占有する」わけではない（そちらは ScrollView 側で判定する）。
    captures_drag = True

    def __init__(self, renderer: 'Renderer', rect: pg.Rect, min_value: float,
                 max_value: float, value: float, step: float = 1,
                 is_float: bool = False, label: str = '') -> None:
        super().__init__(rect)
        self._r = renderer
        self.min_value = min_value
        self.max_value = max_value
        self.step = step
        self.is_float = is_float
        self.label = label
        self.value = self._quantize(value)
        self.changed = False
        self._dragging = False
        self._value_before = self.value
        self._label_text = Text(renderer)
        self._value_text = Text(renderer)

    def _quantize(self, raw: float) -> float:
        steps = round((raw - self.min_value) / self.step)
        v = self.min_value + steps * self.step
        v = max(self.min_value, min(self.max_value, v))
        return round(v, 3) if self.is_float else int(round(v))

    def _value_from_x(self, x: int) -> float:
        ratio = (x - self.rect.x) / max(1, self.rect.width)
        ratio = max(0.0, min(1.0, ratio))
        raw = self.min_value + ratio * (self.max_value - self.min_value)
        return self._quantize(raw)

    def handle_down(self, x: int, y: int) -> bool:
        if not self.visible or not self.rect.collidepoint(x, y):
            return False
        # ここでは値を変えない（クラス docstring 参照）。ScrollView が未決から
        # スクロールと判定して handle_cancel() を呼んだ場合に戻す元の値も、
        # ここで確定させておく。
        self._dragging = True
        self._value_before = self.value
        return True

    def handle_move(self, x: int, y: int) -> None:
        if self._dragging:
            self.value = self._value_from_x(x)

    def handle_up(self, x: int, y: int) -> bool:
        if not self._dragging:
            return False
        self._dragging = False
        # トラックをタップしただけ（MOVE 無し）の操作は、ここで初めて
        # タップ位置の値が確定する。DOWN で値を変えなくした分の担保。
        self.value = self._value_from_x(x)
        self.changed = True
        return True

    def handle_cancel(self) -> None:
        # ScrollView が縦スクロールと判定して掴んでいた子を手放すときに呼ばれる。
        # MOVE で先行して動かしていた値を、掴む前の値へ戻す（changed は立てない）。
        self._dragging = False
        self.value = self._value_before

    def draw(self) -> None:
        if not self.visible:
            return
        # UI_ROW_LABEL_FONT_SIZE は論理px。px() した値をオフセット計算にも使う
        # （見出しの実際の高さ相当を物理pxで揃えるため、変換前の定数を混ぜない）。
        label_font_size = self._r.px(UI_ROW_LABEL_FONT_SIZE)
        if self.label:
            # 基本設定画面の行見出しは UI_ROW_LABEL_FONT_SIZE(24px) に統一する
            # （トグル/スピナー行の Label と揃える）。
            self._label_text.set(self.label, label_font_size)
            self._label_text.draw(self.rect.x, self.rect.y)

        track_h = self._r.px(8)
        label_offset = label_font_size // 2 if self.label else 0
        track_y = self.rect.centery - track_h // 2 + label_offset
        track = pg.Rect(self.rect.x, track_y, self.rect.width, track_h)
        self._r.fill_rect(track, COLOR_TRACK, alpha=200)

        span = max(1e-9, self.max_value - self.min_value)
        ratio = (self.value - self.min_value) / span
        fill_w = int(self.rect.width * ratio)
        self._r.fill_rect(pg.Rect(self.rect.x, track_y, fill_w, track_h), COLOR_ACCENT, alpha=220)

        knob_r = self._r.px(15)
        knob = pg.Rect(self.rect.x + fill_w - knob_r, track_y + track_h // 2 - knob_r,
                       knob_r * 2, knob_r * 2)
        self._r.fill_rect(knob, COLOR_KNOB, alpha=230)

        value_str = f'{self.value:.1f}' if self.is_float else str(self.value)
        self._value_text.set(value_str, self._r.px(UI_VALUE_FONT_SIZE))
        self._value_text.draw_right(self.rect.right, self.rect.y)


class Spinner(Widget):
    """ タップで選択肢を巡回する（`display_mode` のような列挙値用） """

    def __init__(self, renderer: 'Renderer', rect: pg.Rect, options: list[str],
                 value: str, labels: list[str] | None = None) -> None:
        """
        `labels` は `options` と同じ並び・同じ長さの表示名（多言語対応用）。
        省略時は従来どおり内部値をそのまま描く。

        **`self.value` は `labels` を渡しても内部値のまま変えない。**
        `settings.py` の `_process_changes()` は `widget.value` を
        `ConfigManager.set()` にそのまま渡す契約なので、ここで表示名を
        保持すると設定ファイルに翻訳後の文字列（日本語等）が書き込まれてしまう。
        """
        super().__init__(rect)
        self._r = renderer
        self.options = list(options)
        self.value = value if value in self.options else self.options[0]
        self.changed = False
        self._text = Text(renderer)
        # _display_text() の「value が options に無い」警告を同じ値につき1回に
        # 絞るための記録（coding-style.md「毎フレーム出力するログを入れない」。
        # draw() のたびに呼ばれうるため。i18n.py の _warn_once() と同じ理由）。
        self._warned_missing_value: object = None
        self.set_labels(labels)

    def set_labels(self, labels: list[str] | None) -> None:
        """
        表示名を差し替える。長さが `options` と合わない場合は警告して無視する
        （設定不備で描画そのものを落とさないため）。
        """
        if labels is not None and len(labels) != len(self.options):
            logger.warning(
                'Spinner の labels の長さが options と一致しないため無視します: '
                'options=%d labels=%d', len(self.options), len(labels))
            labels = None
        self.labels = labels

    def _display_text(self) -> str:
        if self.labels is not None:
            if self.value in self.options:
                idx = self.options.index(self.value)
                return self.labels[idx]
            # `value` が `options` に無い（壊れた設定ファイル等）。draw() の中で
            # 例外を投げるとメインループごと落ち、`restart: unless-stopped` の
            # 再起動ループになりえる（known-issues.md「`transition` に文字列以外が
            # 入るとクラッシュループしていた」と同じ形）。フォールバックして描く。
            # 警告は同じ値につき1回だけ（draw() は毎フレーム呼ばれるため）。
            if self.value != self._warned_missing_value:
                self._warned_missing_value = self.value
                logger.warning(
                    'Spinner の value が options に見つからないため値をそのまま表示します: '
                    'value=%r options=%r', self.value, self.options)
        return str(self.value)

    def handle_up(self, x: int, y: int) -> bool:
        if not self.visible or not self.rect.collidepoint(x, y):
            return False
        idx = self.options.index(self.value)
        self.value = self.options[(idx + 1) % len(self.options)]
        self.changed = True
        return True

    def draw(self) -> None:
        if not self.visible:
            return
        self._r.fill_rect(self.rect, COLOR_PANEL, alpha=210)
        self._r.draw_rect(self.rect, COLOR_BORDER, alpha=200, width=self._r.px(2))
        self._text.set(self._display_text(), self._r.px(UI_VALUE_FONT_SIZE))
        self._text.draw_centered(self.rect.centerx,
                                 self.rect.y + (self.rect.height - self._text.height) // 2)


class ScrollView(Widget):
    """
    縦スクロール領域。

    **クリップ方式の選択について**: `pygame._sdl2.video.Renderer` には
    `get_viewport` / `set_viewport` があるが、SDL の `SDL_RenderSetViewport` は
    矩形でクリップすると同時に、以降の描画座標の原点もその矩形の左上へ移動させる
    （SDL の仕様）。これを使うには子ウィジェットの描画を毎回ビューポート相対座標へ
    直す必要があり、`handle_down`/`handle_up` の当たり判定用座標（画面座標）と
    二重管理になって複雑さが増す。代わりに「可視範囲外の行は描かない」方式を選んだ。
    子は行単位（`rect.height` ごと）でしか配置されない前提のため、
    行の境界で切っても見た目は破綻しない。

    子の `rect` は content 座標系（y=0 が一番上の子の上端）で持たせる。
    画面座標への変換は draw() 内で一時的に `rect.y` をずらして戻す方式にした
    （呼び出し側にビューポート相対座標を意識させないため）。
    """

    def __init__(self, renderer: 'Renderer', viewport: pg.Rect) -> None:
        super().__init__(viewport)
        self._r = renderer
        self.viewport = viewport
        self.children: list[Widget] = []
        self.content_height = 0
        self.scroll_y = 0

        self._drag_start: tuple[int, int] | None = None
        self._drag_start_scroll = 0
        # 3状態: 未決（どちらもFalse）/ スクロール中（_dragging_scroll）/
        # 子ドラッグ中（_child_captured）。未決の間は閾値を超えるまでどちらへも倒さない。
        self._dragging_scroll = False
        self._child_captured = False
        self._active_child: Widget | None = None

    def add(self, widget: Widget) -> None:
        """ 子を追加する。`widget.rect` は content 座標系で渡すこと """
        self.children.append(widget)
        self.content_height = max(self.content_height, widget.rect.y + widget.rect.height)

    def clear(self) -> None:
        self.children.clear()
        self.content_height = 0
        self.scroll_y = 0

    def visible_range(self) -> tuple[int, int]:
        """ 可視範囲を content 座標の (上端, 下端) で返す。テクスチャ生成/解放判定に使う """
        return self.scroll_y, self.scroll_y + self.viewport.height

    @property
    def max_scroll(self) -> int:
        """
        スクロール可能な最大量（content_height と viewport.height から算出）。

        将来パディングやインセットが増えても式が1箇所で済むよう公開する
        （呼び出し側 = 基本設定画面がこの式を複製すると、その追従漏れが起きるため）。
        """
        return max(0, self.content_height - self.viewport.height)

    def _to_content(self, x: int, y: int) -> tuple[int, int]:
        return x, y - (self.viewport.y - self.scroll_y)

    def handle_down(self, x: int, y: int) -> bool:
        if not self.visible or not self.viewport.collidepoint(x, y):
            return False
        self._drag_start = (x, y)
        self._drag_start_scroll = self.scroll_y
        self._dragging_scroll = False
        self._child_captured = False

        cx, cy = self._to_content(x, y)
        self._active_child = None
        for child in self.children:
            if child.visible and child.rect.collidepoint(cx, cy):
                child.handle_down(cx, cy)
                self._active_child = child
                break
        return True

    def handle_move(self, x: int, y: int) -> None:
        if self._drag_start is None:
            return
        dx = x - self._drag_start[0]
        dy = y - self._drag_start[1]

        # 未決の間（_dragging_scroll も _child_captured も False）は、
        # スクロールも子への MOVE 転送も行わない。指の動きが小さいうちに
        # どちらかへ倒すと、タップのつもりのわずかなぶれで誤判定するため
        # （スクロールに倒せば静止タップが反応しなくなり、子に倒せば
        # Slider が触れただけで動き出す）。閾値を超えるまでは保留する。
        if not self._dragging_scroll and not self._child_captured:
            if max(abs(dx), abs(dy)) > self._r.px(SCROLL_DRAG_THRESHOLD):
                # 閾値を超えた瞬間に一度だけ行き先を決める。
                # 掴んでいる子が captures_drag（Slider）を宣言していて、
                # かつ横方向の移動が縦方向以上（45度ちょうどは `>=` により
                # 子＝Slider を優先する）なら子ドラッグとみなす。
                # そうでなければスクロールに倒し、掴んでいた子を手放して
                # 押下状態や仮の値を handle_cancel() で元に戻す
                # （手放された子は以降 UP を受け取れないため）。
                if (self._active_child is not None and self._active_child.captures_drag
                        and abs(dx) >= abs(dy)):
                    self._child_captured = True
                else:
                    self._dragging_scroll = True
                    if self._active_child is not None:
                        self._active_child.handle_cancel()
                    self._active_child = None
            else:
                return

        if self._dragging_scroll:
            self.scroll_y = max(0, min(self.max_scroll, self._drag_start_scroll - dy))
        elif self._child_captured and self._active_child is not None:
            cx, cy = self._to_content(x, y)
            self._active_child.handle_move(cx, cy)

    def handle_up(self, x: int, y: int) -> Any:
        # 未決のまま UP になった場合（＝ほぼ静止＝タップ）は、従来どおり
        # 子の handle_up を呼ぶ。呼ばないのは _dragging_scroll のときだけ
        # （_child_captured のときは子ドラッグの確定として呼ぶ必要がある）。
        result = None
        if not self._dragging_scroll and self._active_child is not None:
            cx, cy = self._to_content(x, y)
            result = self._active_child.handle_up(cx, cy)
        self._drag_start = None
        self._dragging_scroll = False
        self._child_captured = False
        self._active_child = None
        return result

    def update(self, now: float) -> None:
        for child in self.children:
            child.update(now)

    def draw(self) -> None:
        if not self.visible:
            return
        top, bottom = self.visible_range()
        offset_y = self.viewport.y - self.scroll_y

        for child in self.children:
            if not child.visible:
                continue
            child_top = child.rect.y
            child_bottom = child.rect.y + child.rect.height
            if child_bottom < top or child_top > bottom:
                continue
            # content 座標 -> 画面座標へ一時的にずらして描き、直後に戻す。
            # ScrollView の外から見た child.rect は常に content 座標のままにするため
            # （当たり判定側の変換 _to_content と対にするための取り決め）。
            original_y = child.rect.y
            child.rect.y = original_y + offset_y
            try:
                child.draw()
            finally:
                child.rect.y = original_y
