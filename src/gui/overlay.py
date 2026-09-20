import logging
import time
from typing import TYPE_CHECKING, Any

import pygame as pg

from src import i18n
from src.gui.text import Text
from src.gui.widgets import COLOR_ACCENT

if TYPE_CHECKING:
    from src.config_manager import ConfigManager
    from src.gui.renderer import Renderer

logger = logging.getLogger(__name__)

# ステータス帯・歯車ボタン・（show_comment が偽のときの）下部帯を出しておく秒数
TRANSIENT_DURATION = 5.0
CLOCK_INTERVAL = 1.0
MEMORY_INTERVAL = 3.0

# comment_font_size を基準にした比率（photo-frame 踏襲）
RATIO_COMMENT = 1.0
RATIO_COUNTER = 0.8
RATIO_DATE = 0.6
RATIO_CLOCK = 2.0

# GEAR_SIZE / GEAR_MARGIN / PAD / COUNTDOWN_HEIGHT は論理px（Renderer.px() で
# 物理pxへ変換してから使う）。**RATIO_* とそこから派生する文字サイズはスケール対象外**
# （comment_font_size はユーザーが基本設定画面で決める値のため。
# .claude/plans/peaceful-giggling-parrot.md）。
GEAR_SIZE = 60
GEAR_MARGIN = 10
PAD = 8

# カウントダウンゲージ（次の自動送りまでの経過）。写真の邪魔にならないよう
# 極力細くする。色は widgets.py の COLOR_ACCENT を使い、値を重複定義しない
COUNTDOWN_HEIGHT = 4
COUNTDOWN_TRACK_ALPHA = 90
COUNTDOWN_FILL_ALPHA = 220


def read_vmrss_kb() -> int:
    """ /proc/self/status から VmRSS を読む。psutil は依存に入れない方針 """
    try:
        with open('/proc/self/status', encoding='utf-8') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except OSError:
        pass
    return -1


class Overlay:
    """
    スライドショーの上に重ねる情報表示

    時計・写真カウンタ・説明文・ステータス・メモリ使用量を扱う。
    photo-frame は時計とメモリをどちらも右上に置いて重ねてしまっていたため、
    メモリ表示は時計の下へずらしてある。
    """

    def __init__(self, renderer: 'Renderer', config: 'ConfigManager') -> None:
        self._r = renderer
        self._config = config

        self._status = Text(renderer)
        self._clock = Text(renderer)
        self._memory = Text(renderer)
        self._counter = Text(renderer)
        self._comment = Text(renderer)
        self._date = Text(renderer)

        self._transient_until = 0.0
        self._status_text = ''
        self._next_clock_at = 0.0
        self._next_memory_at = 0.0
        self._gen = -1

        # 写真ごとの表示内容。写真が変わったときだけ組み立て直す
        self._photo_index = -1
        self._photo_total = 0
        self._photo_info: dict[str, Any] | None = None
        self._photo_dirty = True

        # 次の自動送りまでの経過（0.0〜1.0）。None は非表示。
        # Immich 未設定で slideshow を生成できない場合にも溝だけが残らないよう、
        # 「表示するものが無い」ことを None で表す
        self._progress: float | None = None

    # ------------------------------------------------------------------ 外部 API

    def set_status(self, message: str) -> None:
        """ 上部にトースト表示する。TRANSIENT_DURATION 秒で自動的に消える """
        self._status_text = message
        self._transient_until = time.monotonic() + TRANSIENT_DURATION
        logger.info('ステータス: %s', message)

    def show_transient(self) -> None:
        """ 中央タップ時など、オーバーレイを一時的に出す """
        self._transient_until = time.monotonic() + TRANSIENT_DURATION

    def hide_transient(self) -> None:
        self._transient_until = 0.0
        self._status_text = ''

    def set_photo(self, index: int, total: int, info: dict[str, Any] | None) -> None:
        """ 表示中の写真が変わったときに呼ぶ """
        self._photo_index, self._photo_total, self._photo_info = index, total, info
        self._photo_dirty = True

    def set_progress(self, ratio: float | None) -> None:
        """ カウントダウンゲージの経過を更新する。SlideshowScreen からの一方向 push """
        self._progress = ratio

    def invalidate(self) -> None:
        """
        保持しているテクスチャを次の update() で作り直させる。

        基本設定画面で `comment_font_size` / `show_comment` / `show_clock` /
        `show_memory_usage` が変更されたときに呼ぶ。`update()` が
        `Renderer.generation` の変化を検知したときと同じ経路（`_photo_dirty` /
        `_next_clock_at` / `_next_memory_at` をリセットする）を使うことで、
        「内容が変化したときだけ作り直す」契約を崩さずに済ませる。
        """
        self._photo_dirty = True
        self._next_clock_at = 0.0
        self._next_memory_at = 0.0

    @property
    def transient_visible(self) -> bool:
        return time.monotonic() < self._transient_until

    @property
    def gear_rect(self) -> pg.Rect:
        # GEAR_SIZE / GEAR_MARGIN は論理px（.claude/plans/peaceful-giggling-parrot.md）。
        # 歯車はメニューを開く唯一のタッチターゲットなのでスケール対象に含める。
        margin = self._r.px(GEAR_MARGIN)
        size = self._r.px(GEAR_SIZE)
        return pg.Rect(margin, margin, size, size)

    def gear_hit(self, x: int, y: int) -> bool:
        """ 歯車ボタンが表示中で、かつその矩形内か """
        return self.transient_visible and self.gear_rect.collidepoint(x, y)

    # -------------------------------------------------------------------- 更新

    def update(self, now: float) -> None:
        """ 時計とメモリを間隔をあけて更新する。毎フレームは作り直さない """
        base = int(self._config.get('comment_font_size', 24))

        if self._gen != self._r.generation:
            # 消灯からの復帰で SDL を作り直した。保持しているテクスチャは全て無効なので
            # 次の更新契機を待たずに作り直させる
            self._gen = self._r.generation
            self._photo_dirty = True
            self._next_clock_at = 0.0
            self._next_memory_at = 0.0

        if self._config.get('show_clock', True) and now >= self._next_clock_at:
            time_format = self._config.get('time_format', i18n.DEFAULT_TIME_FORMAT)
            self._clock.set(i18n.format_time(None, time_format), int(base * RATIO_CLOCK))
            self._next_clock_at = now + CLOCK_INTERVAL

        if self._config.get('show_memory_usage', False) and now >= self._next_memory_at:
            rss = read_vmrss_kb()
            self._memory.set(f'RSS: {rss // 1024} MB' if rss > 0 else 'RSS: ?',
                             int(base * RATIO_DATE))
            self._next_memory_at = now + MEMORY_INTERVAL

        if self._photo_dirty:
            self._rebuild_photo_texts(base)
            self._photo_dirty = False

        if self._status_text and not self.transient_visible:
            self._status_text = ''

    def _rebuild_photo_texts(self, base: int) -> None:
        """ カウンタ・説明文・日付を組み立てる """
        width = self._r.size[0]

        if self._photo_total > 0:
            self._counter.set(f'{self._photo_index + 1}/{self._photo_total}',
                              int(base * RATIO_COUNTER))
        else:
            self._counter.set('', int(base * RATIO_COUNTER))

        info = self._photo_info or {}

        # [アルバム名] は source が album / daily_pickup のときだけ前置する。
        # daily_pickup は写真ごとのアルバム名を優先する（immich_api が付与する）。
        source = self._config.get('source')
        parts = []
        if source in ('album', 'daily_pickup'):
            album_name = info.get('album_name') or self._config.get('album_name', '')
            if album_name:
                parts.append(f'[{album_name}]')
        if info.get('description'):
            parts.append(info['description'])
        comment = ' '.join(parts)

        date_text = self._format_date(info.get('date'))
        date_size = int(base * RATIO_DATE)
        counter_w = self._counter.width
        self._date.set(date_text, date_size)

        # 説明文はカウンタと日付の残り幅に収める（PAD は draw() 側の余白と
        # 同じ物理pxで揃える必要があるため px() を通す）
        available = width - counter_w - self._date.width - self._r.px(PAD) * 4
        self._comment.set(self._fit(comment, base, max(0, available)), base)

    def _format_date(self, raw: str | None) -> str:
        """
        写真の撮影日（Immich の ISO8601、例: 2024-03-12T15:55:18.092Z）を整形する。

        メソッド自体は残し、config から date_format を読む場所をここ1か所に保つ
        （実際の整形は i18n.format_date() に委ねる。.claude/plans 参照）。
        """
        date_format = self._config.get('date_format', i18n.DEFAULT_DATE_FORMAT)
        return i18n.format_date(raw, date_format)

    def _fit(self, text: str, size: int, max_width: int) -> str:
        """ 幅に収まるよう末尾を省略する。二分探索なので写真1枚あたり数回で済む """
        if not text or max_width <= 0:
            return ''
        font = self._r.font(size)
        if font.size(text)[0] <= max_width:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.size(text[:mid] + '…')[0] <= max_width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + '…' if lo else ''

    # -------------------------------------------------------------------- 描画

    def draw(self) -> None:
        width, height = self._r.size
        base = int(self._config.get('comment_font_size', 24))
        transient = self.transient_visible
        # PAD は論理px（文字サイズ由来ではない寸法定数）。overlay.py の docstring 方針
        # どおりスケール対象に含める
        pad = self._r.px(PAD)

        if self._config.get('show_clock', True):
            self._clock.draw(width - self._clock.width - pad * 2, pad, alpha=230)

        if self._config.get('show_memory_usage', False):
            top = pad + (self._clock.height if self._config.get('show_clock', True) else 0)
            self._memory.draw(width - self._memory.width - pad * 2, top + pad, alpha=180)

        if self._bottom_bar_visible():
            self._draw_bottom_bar(width, height, base)

        if self._config.get('show_countdown', True):
            self._draw_countdown(width, height, base)

        if transient:
            self._draw_status_bar(width, height)
            self._draw_gear()

    def _bottom_bar_height(self, base: int) -> int:
        """
        下部帯（カウンタ・説明文・日付を表示する帯）の高さを、文字サイズ `base`
        （`comment_font_size` 由来）から算出する。

        **意図的に `Renderer.px()` を通さず、スケール対象外にしてある。**
        下部帯の高さは中に入る文字の大きさに連動すべきもので、その文字
        （`comment_font_size`）自体が SPECIFICATION.md 5.5 でスケール対象外と
        決めた値である以上、帯の高さも一緒にスケールしないのが一貫している。
        ここで `+ 10` を `self._r.px(10)` に変えると、`comment_font_size` は
        変わらないまま帯だけが高解像度で厚くなり、文字が帯の中央から浮いて見える
        （実測: comment_font_size=24 のとき、`px(10)` 化すると帯高は
        1024x600/1920x1080/3840x2160 で 46/54/72px と変わるが、文字は 24px の
        ままずれる。素直な `px()` に戻さないこと）。
        `comment_font_size` をユーザーが上げれば帯もこの式で自動的に追従する。

        なお左右の余白 `PAD` は `px()` でスケールしているため、下部帯は
        「左右の余白は解像度に追従し、上下の高さは文字サイズに追従する」という
        非対称な扱いになっている。これは既知の不整合として受け入れている
        （左右も文字サイズ連動にする理由が無く、さりとて上下だけ `px()` すると
        上の理由で文字が浮くため）。
        """
        return int(base * 1.5) + 10

    def _bottom_bar_visible(self) -> bool:
        """
        下部帯が実際に描かれるか。

        従来 `draw()` にあった「`show_comment` が真、または一時表示中」と、
        `_draw_bottom_bar()` の内側にあった「表示する内容があるか」の2条件を
        1つにまとめた。カウントダウンゲージの Y 座標が下部帯の高さに連動するため、
        外（`_draw_countdown()`）からも判定できるよう切り出す。
        """
        has_content = bool(self._counter.width or self._comment.width or self._date.width)
        return (self._config.get('show_comment', True) or self.transient_visible) and has_content

    def _draw_bottom_bar(self, width: int, height: int, base: int) -> None:
        pad = self._r.px(PAD)
        bar_h = self._bottom_bar_height(base)
        top = height - bar_h
        self._r.fill_rect(pg.Rect(0, top, width, bar_h), (0, 0, 0), alpha=150)

        text_y = top + (bar_h - self._comment.height) // 2 if self._comment.height else top
        self._counter.draw(pad, top + (bar_h - self._counter.height) // 2)
        self._comment.draw(self._counter.width + pad * 3, text_y)
        self._date.draw(width - self._date.width - pad,
                        top + (bar_h - self._date.height) // 2)

    def _draw_countdown(self, width: int, height: int, base: int) -> None:
        """
        次の自動送りまでの経過を示す 4px のゲージ。

        テクスチャは作らない（フルスクリーンの毎フレーム再生成を避ける禁止パターンに
        抵触しないよう、矩形塗り2枚だけで構成する）。下部帯（コメント欄）が出ている
        場合はその直上へ、出ていなければ画面最下部に接する。
        """
        if self._progress is None:
            return
        countdown_height = self._r.px(COUNTDOWN_HEIGHT)
        bottom = height - self._bottom_bar_height(base) if self._bottom_bar_visible() else height
        top = bottom - countdown_height

        self._r.fill_rect(pg.Rect(0, top, width, countdown_height), (0, 0, 0),
                          alpha=COUNTDOWN_TRACK_ALPHA)
        fill_w = int(width * self._progress)
        if fill_w > 0:
            self._r.fill_rect(pg.Rect(0, top, fill_w, countdown_height), COLOR_ACCENT,
                              alpha=COUNTDOWN_FILL_ALPHA)

    def _draw_status_bar(self, width: int, height: int) -> None:
        bar_h = max(int(height * 0.10), self._r.px(GEAR_SIZE) + self._r.px(GEAR_MARGIN) * 2)
        self._r.fill_rect(pg.Rect(0, 0, width, bar_h), (0, 0, 0), alpha=130)
        if not self._status_text:
            return
        self._status.set(self._status_text, int(self._config.get('comment_font_size', 24)))
        self._status.draw((width - self._status.width) // 2,
                          (bar_h - self._status.height) // 2, alpha=220)

    def _draw_gear(self) -> None:
        """
        メニューを開くボタン。3本線で描く。

        絵文字はフォントに含まれるかがフォント依存になるため、図形で描いて
        確実に表示されるようにしている。
        """
        rect = self.gear_rect
        self._r.fill_rect(rect, (255, 255, 255), alpha=40)
        # 歯車内部の3本線のオフセットも GEAR_SIZE と同じ倍率で拡大する
        # （そうしないと歯車の枠だけ大きくなり、中の線が相対的に小さいまま残る）
        px = self._r.px
        line_w = rect.width - px(24)
        line_h = px(4)
        x = rect.x + px(12)
        for i in range(3):
            y = rect.y + px(18) + i * px(10)
            self._r.fill_rect(pg.Rect(x, y, line_w, line_h), (255, 255, 255), alpha=230)
