"""
テキスト1つ分のテクスチャを管理する部品

`overlay.py` にあったプライベートクラス `_Text` を切り出して一般化したもの。
ウィジェット層（`widgets.py`）でもラベルの描画に使い回すため、
`overlay.py` 専用だった置き場所から独立させた。振る舞いは変えていない。
"""

import logging
from typing import TYPE_CHECKING

import pygame as pg

if TYPE_CHECKING:
    from src.gui.renderer import Renderer

logger = logging.getLogger(__name__)


class Text:
    """
    テキスト1つ分のテクスチャを持つ。

    **内容・サイズ・色・SDL の世代のいずれかが変わったときだけ作り直す。**
    毎フレームの再生成は禁止パターン（.claude/architecture.md）。
    """

    def __init__(self, renderer: 'Renderer') -> None:
        self._r = renderer
        self._text: str | None = None
        self._size: int | None = None
        self._color: tuple[int, int, int] | None = None
        self._gen = -1
        self._tex = None
        self.width = 0
        self.height = 0

    def set(self, text: str, size: int,
            color: tuple[int, int, int] = (255, 255, 255)) -> None:
        """ 内容・サイズ・色・世代がすべて同じなら何もしない（再生成を避ける） """
        if (text == self._text and size == self._size and color == self._color
                and self._gen == self._r.generation):
            return

        self._text, self._size, self._color = text, size, color
        self._gen = self._r.generation

        result = self._r.text_texture(text, size, color) if text else None
        if result is None:
            self._tex, self.width, self.height = None, 0, 0
            return
        self._tex, self.width, self.height = result
        self._tex.blend_mode = pg.BLENDMODE_BLEND

    def draw(self, x: int, y: int, alpha: int = 255) -> None:
        """ 左上を (x, y) に置いて描く """
        # 世代が変わったテクスチャは SDL 側で無効になっている。
        # set() で作り直されるまで描かない（触ると Parameter 'texture' is invalid で落ちる）。
        if self._tex is None or self._gen != self._r.generation:
            return
        self._tex.alpha = alpha
        self._tex.draw(dstrect=pg.Rect(x, y, self.width, self.height))

    def draw_right(self, right: int, y: int, alpha: int = 255) -> None:
        """ 右端を right に合わせて描く（ウィジェットの右寄せラベルで使う） """
        self.draw(right - self.width, y, alpha)

    def draw_centered(self, center_x: int, y: int, alpha: int = 255) -> None:
        """ 水平方向の中心を center_x に合わせて描く（ボタンの中央揃えラベルで使う） """
        self.draw(center_x - self.width // 2, y, alpha)


def wrap_lines(renderer: 'Renderer', text: str, size: int, max_width: int,
               max_lines: int) -> list[str]:
    """
    指定幅・最大行数に収まるよう文字列を行分割する。

    `overlay.py` の `_fit()`（二分探索による末尾省略）と同じ考え方を
    複数行へ拡張したもの。日本語には英語のような単語境界が無いため、
    分かち書きではなく**文字単位**で詰める。幅の計測は `_fit()` と同じく
    `Renderer.font(size).size()` を使う。

    最大行数に収まらない分は最終行の末尾を「…」で省略する
    （`_fit()` と同じ省略記号・同じ二分探索）。呼び出し側で結果をキャッシュし、
    毎フレーム呼ばないこと（禁止パターン: オーバーレイの毎フレーム再描画と同じ理由）。
    """
    if not text or max_width <= 0 or max_lines <= 0:
        return []
    font = renderer.font(size)

    lines: list[str] = []
    remaining = text
    while remaining and len(lines) < max_lines:
        is_last_allowed = len(lines) == max_lines - 1

        if font.size(remaining)[0] <= max_width:
            # 残り全部がこの行に収まる（最終行かどうかによらず、これで打ち切り）
            lines.append(remaining)
            remaining = ''
            break

        if is_last_allowed:
            # 最終行なのに収まらない分が残る -> 末尾を省略する（_fit() と同じ二分探索）
            lo, hi = 0, len(remaining)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.size(remaining[:mid] + '…')[0] <= max_width:
                    lo = mid
                else:
                    hi = mid - 1
            # lo == 0 は「省略記号1文字ぶんの幅すら無い」極端な狭さ。
            # `_fit()` はこの場合に空文字を返して何も描かない設計になっており
            # （`…` 自体が max_width を超えてはみ出すのを避けるため）、
            # ここでもその判断をそのまま引き継ぐ。
            lines.append(remaining[:lo] + '…' if lo else '')
            remaining = ''
            break

        # 最終行でなければ、この行に入るだけ詰めて次行へ回す
        lo, hi = 1, len(remaining)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.size(remaining[:mid])[0] <= max_width:
                lo = mid
            else:
                hi = mid - 1
        cut = max(1, lo)  # 1文字も入らない極端な幅でも必ず前進させる
        lines.append(remaining[:cut])
        remaining = remaining[cut:]

    return lines
