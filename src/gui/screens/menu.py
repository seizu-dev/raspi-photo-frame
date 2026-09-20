"""
メニュー画面

ヘッダ（戻る / タイトル）と、縦に並ぶ3ボタン（基本設定 / アルバム選択 /
アプリを終了）だけの画面。項目と文言は photo-frame の `menuscreen.kv` /
`MenuScreen` を踏襲しているが、Kivy の Screen/BoxLayout 設計は流用していない
（.claude/architecture.md「参照元」の方針どおり）。
"""

import logging
from typing import TYPE_CHECKING

import pygame as pg

from src import i18n
from src.gui.renderer import TAP_DOWN, TAP_UP
from src.gui.screens import ACTION_ALBUM, ACTION_BACK, ACTION_QUIT, ACTION_SETTINGS
from src.gui.widgets import MIN_HIT_SIZE, UI_FONT_SIZE, Button, Label
from src.i18n import t

if TYPE_CHECKING:
    from src.gui.renderer import Renderer

logger = logging.getLogger(__name__)

HEADER_HEIGHT = 80
BUTTON_HEIGHT = 80
BUTTON_SPACING = 30
BUTTON_WIDTH_RATIO = 0.6
BACK_BUTTON_WIDTH = 120
PAD = 10


class MenuScreen:
    """ 基本設定 / アルバム選択 / 終了 への入口となるメニュー画面 """

    def __init__(self, renderer: 'Renderer') -> None:
        self._r = renderer
        self._gen = -1
        self._back_button: Button | None = None
        self._title: Label | None = None
        self._buttons: list[Button] = []
        self._build()

    def _build(self) -> None:
        """
        ウィジェットを組み立てる（初回、または SDL 再生成後に呼ぶ）。

        消灯からの復帰で `Renderer` が作り直されると、ここで持つ `Text` の
        テクスチャも無効になる。`Text` 自体は `set()` の際に世代を見て
        作り直すため、`Button`/`Label` を作り直す必要は無いが、`Renderer` の
        `size` はここでしか参照しないため、解像度が変わる可能性に備えて
        レイアウトごと作り直す形にしておく（実運用では解像度は固定だが、
        `Renderer.create()` の度に呼ぶ設計にしておくと安全側に倒せる）。
        """
        width, height = self._r.size
        px = self._r.px

        header_height = px(HEADER_HEIGHT)
        pad = px(PAD)
        back_button_width = px(BACK_BUTTON_WIDTH)
        button_height = px(BUTTON_HEIGHT)
        button_spacing = px(BUTTON_SPACING)
        min_hit_size = px(MIN_HIT_SIZE)

        self._back_button = Button(
            self._r, pg.Rect(pad, pad, back_button_width, header_height - pad * 2),
            t('common.back'), action=ACTION_BACK, font_size=UI_FONT_SIZE)
        self._title = Label(self._r, pg.Rect(0, pad, width, header_height - pad * 2),
                            t('menu.title'), font_size=UI_FONT_SIZE + 8, centered=True)

        button_width = max(int(width * BUTTON_WIDTH_RATIO), min_hit_size * 2)
        button_x = (width - button_width) // 2
        # 表示名は _build() の中で解決する（モジュール読み込み時に固定すると
        # 言語を切り替えても古い文字列のまま残る）。
        entries = [
            (t('menu.settings'), ACTION_SETTINGS),
            (t('menu.album'), ACTION_ALBUM),
            (t('menu.quit'), ACTION_QUIT),
        ]
        total_height = len(entries) * button_height + (len(entries) - 1) * button_spacing
        start_y = header_height + max(0, (height - header_height - total_height) // 2)

        self._buttons = []
        for i, (text, action) in enumerate(entries):
            y = start_y + i * (button_height + button_spacing)
            rect = pg.Rect(button_x, y, button_width, button_height)
            self._buttons.append(Button(self._r, rect, text, action=action, font_size=UI_FONT_SIZE))

        # SDL の世代と言語の世代の組で持つ（i18n.generation ↔ 3画面の
        # _recreate_if_needed の対。.claude/architecture.md「対で更新が必要な箇所」参照）。
        self._gen = (self._r.generation, i18n.generation())

    def _recreate_if_needed(self) -> None:
        if self._gen != (self._r.generation, i18n.generation()):
            self._build()

    # ------------------------------------------------------------------ 契約

    def on_enter(self) -> None:
        pass

    def on_leave(self) -> None:
        pass

    def update(self, now: float) -> None:
        self._recreate_if_needed()

    def draw(self) -> None:
        self._recreate_if_needed()
        # スライドショーの上に重ねるのではなく、単独で背景を塗ってから描く。
        # メニュー階層を開いている間はスライドショーを描かない（main.py 側の方針）ため
        # 背景が透けて何も無い状態にならないよう、ここで塗りつぶす。
        self._r.fill_rect(pg.Rect(0, 0, *self._r.size), (20, 20, 20), alpha=255)
        self._back_button.draw()
        self._title.draw()
        for button in self._buttons:
            button.draw()

    def handle_input(self, kind: str, x: int, y: int) -> str | None:
        if kind == TAP_DOWN:
            self._back_button.handle_down(x, y)
            for button in self._buttons:
                button.handle_down(x, y)
            return None

        if kind == TAP_UP:
            if self._back_button.handle_up(x, y):
                return ACTION_BACK
            for button in self._buttons:
                if button.handle_up(x, y):
                    return button.action
            return None

        return None
