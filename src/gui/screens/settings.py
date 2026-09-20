"""
基本設定画面

`ScrollView` に設定項目を縦に並べる。ヘッダ（戻る / タイトル）は `menu.py` と
揃えている。画面自身は他のコンポーネント（`slideshow` / `Overlay` / `display` /
`cache` 等）を直接触らず、値が変わったキーを `on_changed` コールバックで
`main.py` へ渡すだけにする（即時反映の実処理は App 側に集約する設計）。

項目数が増えたため、見出し＋区切り線でグループ分けして並べている
（`_ROWS` の種別 `'header'`）。見出し行は設定値を持たないため `_controls` には
入れず、`_recreate_if_needed` / `_process_changes` の走査対象からも外れる。

**出さない項目**（意図的な除外。増やさないこと。理由は
`.claude/plans/delegated-leaping-perlis.md` ステップ2を参照）:
- `daily_pickup_date` / `daily_pickup_selected_ids` / `daily_pickup_remaining_ids`
  （永続化された実行時状態。SPECIFICATION.md 7.4）
- `source` / `album_id` / `album_name`（アルバム選択画面の担当）
- `max_slides_in_memory`（実装が無い/未参照の死んだキー）
"""

import logging
from typing import TYPE_CHECKING, Callable

import pygame as pg

from src import i18n
from src.gui.renderer import TAP_DOWN, TAP_MOVE, TAP_UP
from src.gui.screens import ACTION_BACK
from src.gui.text import Text
from src.gui.transitions import ALL_VALUES as TRANSITION_VALUES
from src.gui.widgets import (
    COLOR_ACCENT,
    COLOR_BORDER,
    MIN_HIT_SIZE,
    UI_FONT_SIZE,
    UI_ROW_LABEL_FONT_SIZE,
    Button,
    Label,
    ScrollView,
    Slider,
    Spinner,
    Toggle,
    Widget,
)
from src.i18n import t
from src.photo_cache import FIT_VALUES

if TYPE_CHECKING:
    from src.config_manager import ConfigManager
    from src.gui.renderer import Renderer

logger = logging.getLogger(__name__)

HEADER_HEIGHT = 80
PAD = 10
CONTENT_PAD = 24
BACK_BUTTON_WIDTH = 120

ROW_SPACING = 16
# 24px の行見出し（Slider.draw() 内）とつまみ（直径30px）が重ならないよう、
# 見出しを20→24pxへ揃えた際に90→100へ広げた（実測: NotoSansCJK 24px の
# テキスト高さは35px。widget高さ84pxのときトラック上端との間に約4pxの余白が残る）。
SLIDER_ROW_HEIGHT = 100
SWITCH_ROW_HEIGHT = 76

TOGGLE_WIDTH = 110
TOGGLE_HEIGHT = MIN_HIT_SIZE
SPINNER_WIDTH = 260
SPINNER_HEIGHT = MIN_HIT_SIZE

# グループ見出し（_SectionHeader）のレイアウト定数。
# 画面ヘッダ用の HEADER_HEIGHT と名前が衝突しないよう SECTION_ を付ける。
SECTION_HEADER_FONT_SIZE = 22
# 実測: NotoSansCJK 22px のテキスト高さは32px。区切り線(2px)との間に6pxの
# 余白を確保して 32+6+2=40、末尾の ROW_SPACING(16) を加えて 56。
SECTION_HEADER_HEIGHT = 56
SECTION_HEADER_LINE_HEIGHT = 2
# 2番目以降のグループ見出しの直前に足す追加の余白（グループの切れ目を
# 分かりやすくするため）。先頭グループ（y==0 のとき）には付けない。
SECTION_HEADER_TOP_MARGIN = 20

# (種別, 設定キー, 表示名の翻訳キー, ウィジェット固有の引数) の並び。
# 種別 'header' はグループ見出し（キー・引数は使わないので '' / {} を入れる）。
# **表示名は i18n の翻訳キー。** 直接文字列を書くとモジュール読み込み時の言語で
# 固定され、切り替えても古いままになる。解決は SettingsScreen._build() の中で行う
# （.claude/architecture.md「対で更新が必要な箇所」参照）。
# キー・表示名・ウィジェット引数は変えず、グループ（言語と表示形式 / スライドショー /
# 画面表示 / 省電力 / 写真の取得）ごとにまとめて並べ替えている。
# 範囲 (min/max) は画面側の入力制約に過ぎず、config_manager.py の既定値とは独立。
# spinner の Spinner.labels は _build() が「value.{設定キー}.{内部値}」の規則で
# 機械的に組み立てる（i18n.py の value.* キーがこの規則に揃えてある）。
_ROWS: list[tuple[str, str, str, dict]] = [
    ('header', '', 'settings.group.locale', {}),
    ('spinner', 'language', 'settings.row.language',
     dict(options=list(i18n.LANGUAGES))),
    ('spinner', 'time_format', 'settings.row.time_format',
     dict(options=list(i18n.TIME_FORMATS))),
    ('spinner', 'date_format', 'settings.row.date_format',
     dict(options=list(i18n.DATE_FORMATS))),

    ('header', '', 'settings.group.slideshow', {}),
    ('slider', 'interval', 'settings.row.interval',
     dict(min_value=1, max_value=60, step=1)),
    ('spinner', 'transition', 'settings.row.transition',
     dict(options=list(TRANSITION_VALUES))),
    ('slider', 'transition_duration', 'settings.row.transition_duration',
     dict(min_value=0.1, max_value=5.0, step=0.1, is_float=True)),
    ('spinner', 'display_mode', 'settings.row.display_mode',
     dict(options=['sequential', 'random'])),

    ('header', '', 'settings.group.display', {}),
    ('spinner', 'photo_fit', 'settings.row.photo_fit',
     dict(options=list(FIT_VALUES))),
    ('toggle', 'show_clock', 'settings.row.show_clock', {}),
    ('toggle', 'show_comment', 'settings.row.show_comment', {}),
    ('slider', 'comment_font_size', 'settings.row.comment_font_size',
     dict(min_value=12, max_value=48, step=4)),
    ('toggle', 'show_countdown', 'settings.row.show_countdown', {}),
    ('toggle', 'show_memory_usage', 'settings.row.show_memory_usage', {}),

    ('header', '', 'settings.group.power', {}),
    ('toggle', 'power_saving_enabled', 'settings.row.power_saving_enabled', {}),
    ('slider', 'power_saving_timeout', 'settings.row.power_saving_timeout',
     dict(min_value=30, max_value=1800, step=30)),
    ('slider', 'display_wakeup_delay', 'settings.row.display_wakeup_delay',
     dict(min_value=0.0, max_value=10.0, step=0.5, is_float=True)),
    ('toggle', 'motion_sensor_enabled', 'settings.row.motion_sensor_enabled', {}),

    ('header', '', 'settings.group.photos', {}),
    ('slider', 'daily_pickup_count', 'settings.row.daily_pickup_count',
     dict(min_value=1, max_value=10, step=1)),
    ('slider', 'cache_lifetime_hours', 'settings.row.cache_lifetime_hours',
     dict(min_value=1, max_value=168, step=1)),
    # 0 は「無制限」の意味を持つ特別値。Slider 自体は数値をそのまま表示するため、
    # 意味は表示名側に埋め込んでおく（値ラベルは "0" のまま出るが、
    # 隣に説明があるので読める。i18n.py の settings.row.photo_cache_max_mb を参照）。
    ('slider', 'photo_cache_max_mb', 'settings.row.photo_cache_max_mb',
     dict(min_value=0, max_value=2048, step=64)),
]


class _SectionHeader(Widget):
    """
    設定のグループ見出し（見出し文字＋区切り線）。

    値を持たないため `_controls` には入れない（`_recreate_if_needed` /
    `_process_changes` が値を持つウィジェットだけを走査する契約を保つため）。
    当たり判定は不要なので `Widget` の既定（何も受理しない）のままでよい。
    """

    def __init__(self, renderer: 'Renderer', rect: pg.Rect, text: str) -> None:
        super().__init__(rect)
        self._r = renderer
        self._label_str = text
        self._text = Text(renderer)

    def draw(self) -> None:
        if not self.visible:
            return
        # SECTION_HEADER_FONT_SIZE / SECTION_HEADER_LINE_HEIGHT は論理px。
        # Renderer.fill_rect() 等を直接使うので px() は自前で通す
        # （Label 経由ではないため Label.draw() の変換に相乗りできない）。
        self._text.set(self._label_str, self._r.px(SECTION_HEADER_FONT_SIZE), COLOR_ACCENT)
        self._text.draw(self.rect.x, self.rect.y)
        line_h = self._r.px(SECTION_HEADER_LINE_HEIGHT)
        line = pg.Rect(self.rect.x, self.rect.bottom - line_h, self.rect.width, line_h)
        self._r.fill_rect(line, COLOR_BORDER, alpha=120)


class SettingsScreen:
    """ 基本設定画面。値が変わったキーを `on_changed` で通知するだけの画面 """

    def __init__(self, renderer: 'Renderer', config: 'ConfigManager',
                 on_changed: Callable[[str], None] | None = None) -> None:
        self._r = renderer
        self._config = config
        self._on_changed = on_changed
        self._gen = -1

        self._back_button: Button | None = None
        self._title: Label | None = None
        self._scroll: ScrollView | None = None
        # (設定キー, 値を持つウィジェット) の一覧。handle_input で changed を見て回る
        self._controls: list[tuple[str, Widget]] = []

        self._build()

    # ------------------------------------------------------------------ 構築

    def _build(self) -> None:
        """
        ウィジェットを組み立てる（初回、または SDL 再生成後に呼ぶ）。

        `Text` は `set()` の中で `Renderer.generation` を見て自動的にテクスチャを
        作り直すため、ここでの再構築自体はテクスチャの安全性には関係しない
        （`menu.py` と同じ理由でレイアウトの作り直しとして行っている）。
        スクロール位置と各ウィジェットの現在値は `_recreate_if_needed` 側で
        退避・復元する。
        """
        width, height = self._r.size
        px = self._r.px

        header_height = px(HEADER_HEIGHT)
        pad = px(PAD)
        content_pad = px(CONTENT_PAD)
        back_button_width = px(BACK_BUTTON_WIDTH)
        row_spacing = px(ROW_SPACING)
        slider_row_height = px(SLIDER_ROW_HEIGHT)
        switch_row_height = px(SWITCH_ROW_HEIGHT)
        toggle_width = px(TOGGLE_WIDTH)
        toggle_height = px(TOGGLE_HEIGHT)
        spinner_width = px(SPINNER_WIDTH)
        spinner_height = px(SPINNER_HEIGHT)
        section_header_height = px(SECTION_HEADER_HEIGHT)
        section_header_top_margin = px(SECTION_HEADER_TOP_MARGIN)

        self._back_button = Button(
            self._r, pg.Rect(pad, pad, back_button_width, header_height - pad * 2),
            t('common.back'), action=ACTION_BACK, font_size=UI_FONT_SIZE)
        self._title = Label(
            self._r, pg.Rect(0, pad, width, header_height - pad * 2),
            t('settings.title'), font_size=UI_FONT_SIZE + 8, centered=True)

        viewport = pg.Rect(0, header_height, width, height - header_height)
        self._scroll = ScrollView(self._r, viewport)
        self._controls = []

        content_width = max(1, width - content_pad * 2)
        label_width = int(content_width * 0.6)

        y = 0
        for kind, key, label_key, kwargs in _ROWS:
            # 表示名は _ROWS では翻訳キーのまま持っているので、ここで初めて
            # 現在言語の文字列へ解決する（モジュール読み込み時に解決しない理由は
            # _ROWS 定義の直前のコメントを参照）。
            label_text = t(label_key)

            if kind == 'header':
                # 先頭グループ（y==0）だけ上余白を詰める。2番目以降は
                # SECTION_HEADER_TOP_MARGIN を足してグループの切れ目を分かりやすくする。
                if y > 0:
                    y += section_header_top_margin
                header = _SectionHeader(
                    self._r, pg.Rect(content_pad, y, content_width,
                                     section_header_height - row_spacing),
                    label_text)
                self._scroll.add(header)
                # 値を持たないため _controls には入れない（クラス docstring 参照）。
                y += section_header_height
                continue

            value = self._config.get(key)

            if kind == 'slider':
                row_h = slider_row_height
                widget = Slider(
                    self._r, pg.Rect(content_pad, y, content_width, row_h - row_spacing),
                    value=value, label=label_text, **kwargs)
                self._scroll.add(widget)

            elif kind == 'toggle':
                row_h = switch_row_height
                label = Label(
                    self._r, pg.Rect(content_pad, y, label_width, row_h - row_spacing),
                    label_text, font_size=UI_ROW_LABEL_FONT_SIZE)
                ctrl_y = y + (row_h - row_spacing - toggle_height) // 2
                widget = Toggle(
                    self._r,
                    pg.Rect(content_pad + content_width - toggle_width, ctrl_y,
                            toggle_width, toggle_height),
                    value=bool(value))
                self._scroll.add(label)
                self._scroll.add(widget)

            elif kind == 'spinner':
                row_h = switch_row_height
                label = Label(
                    self._r, pg.Rect(content_pad, y, label_width, row_h - row_spacing),
                    label_text, font_size=UI_ROW_LABEL_FONT_SIZE)
                ctrl_y = y + (row_h - row_spacing - spinner_height) // 2
                # 表示名は「value.{設定キー}.{内部値}」の規則で機械的に組み立てる
                # （transition / display_mode / photo_fit / language / time_format /
                # date_format のすべてがこの規則で i18n.py に揃えてある）。
                # Spinner.value は内部値のままにする契約なので labels は表示専用。
                options = kwargs.get('options', [])
                spinner_labels = [t(f'value.{key}.{v}') for v in options]
                widget = Spinner(
                    self._r,
                    pg.Rect(content_pad + content_width - spinner_width, ctrl_y,
                            spinner_width, spinner_height),
                    value=value, labels=spinner_labels, **kwargs)
                self._scroll.add(label)
                self._scroll.add(widget)

            else:  # pragma: no cover - _ROWS はこのモジュール内で固定
                logger.warning('未知のウィジェット種別です: %s', kind)
                continue

            self._controls.append((key, widget))
            y += row_h

        # SDL の世代と言語の世代の組で持つ（i18n.generation ↔ 3画面の
        # _recreate_if_needed の対。片方だけ見ると言語を変えてもこの画面だけ
        # 古い言語のまま残る。.claude/architecture.md「対で更新が必要な箇所」参照）。
        self._gen = (self._r.generation, i18n.generation())

    def _recreate_if_needed(self) -> None:
        if self._gen == (self._r.generation, i18n.generation()):
            return
        # 消灯復帰での SDL 再生成、または言語切替のどちらか。
        # どちらの場合もウィジェットの現在値とスクロール位置を退避してから作り直し、
        # 直後に復元する（言語を変えてもスクロール位置と未保存の変更が飛ばないため）。
        # （config には既に保存済みの値なので config から読み直しても本来は
        # 同じになるはずだが、ドラッグ未確定分の見た目のズレを避けるため
        # ウィジェット側の値を優先する）。
        values = {key: widget.value for key, widget in self._controls}
        scroll_y = self._scroll.scroll_y if self._scroll else 0

        self._build()

        for key, widget in self._controls:
            if key in values and hasattr(widget, 'value'):
                widget.value = values[key]
        if self._scroll is not None:
            # 最大スクロール量は ScrollView.max_scroll を参照する（式の複製を避ける。
            # .claude/plans/delegated-leaping-perlis.md 参照）。
            self._scroll.scroll_y = min(scroll_y, self._scroll.max_scroll)

    def _process_changes(self) -> None:
        """ UP イベントの後に呼ぶ。値が変わったウィジェットだけ保存して通知する """
        for key, widget in self._controls:
            if not getattr(widget, 'changed', False):
                continue
            widget.changed = False
            value = widget.value
            self._config.set(key, value)
            logger.info('設定を変更しました: %s=%r', key, value)
            if self._on_changed is not None:
                self._on_changed(key)

    # ------------------------------------------------------------------ 契約

    def on_enter(self) -> None:
        pass

    def on_leave(self) -> None:
        pass

    def update(self, now: float) -> None:
        self._recreate_if_needed()
        self._scroll.update(now)

    def draw(self) -> None:
        self._recreate_if_needed()
        width, _height = self._r.size
        self._r.fill_rect(pg.Rect(0, 0, *self._r.size), (20, 20, 20), alpha=255)

        # album.py と同じ理由でヘッダを最後に描く（.claude/plans/delegated-leaping-perlis.md
        # 修正1）。ScrollView が可視行だけ描く方式のため、行の一部が viewport の
        # 上端をはみ出してヘッダを覆うことがある。
        self._scroll.draw()

        self._r.fill_rect(pg.Rect(0, 0, width, self._r.px(HEADER_HEIGHT)), (20, 20, 20), alpha=255)
        self._back_button.draw()
        self._title.draw()

    def handle_input(self, kind: str, x: int, y: int) -> str | None:
        self._recreate_if_needed()

        if kind == TAP_DOWN:
            self._back_button.handle_down(x, y)
            self._scroll.handle_down(x, y)
            return None

        if kind == TAP_MOVE:
            self._scroll.handle_move(x, y)
            return None

        if kind == TAP_UP:
            if self._back_button.handle_up(x, y):
                return ACTION_BACK
            self._scroll.handle_up(x, y)
            self._process_changes()
            return None

        return None
