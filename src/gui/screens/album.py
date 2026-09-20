"""
アルバム選択画面

`ScrollView` に4列のサムネイルグリッドを載せる。**同時に常駐させるテクスチャは
可視範囲＋前後1行に限定する**（.claude/architecture.md の「テクスチャを3枚を超えて
常駐させない」はスライドショー本体の制約だが、この画面でも「画面外のテクスチャを
持ち続けるとアルバム数に比例してメモリを食う」という同種の問題があるため、
プラン（delegated-leaping-perlis.md ステップ3）の指示どおり明示的に上限を設ける。
実際のセルサイズ（幅約232px・行高約248px）と viewport（高さ約520px）で試算すると、
可視範囲に収まる行に前後1行を加えた範囲は4行 x 4列 = 最大16枚程度になる
（画面解像度やレイアウト定数を変えると数字も変わる、あくまで目安）。
1枚あたり数十KB程度のサムネイルなので、それでも数MB程度に収まる）。

写真本体と同じく、ネットワーク I/O とディスク I/O はワーカースレッドで行い、
**テクスチャ生成（`Renderer.texture_from_image`）だけをメインスレッドで行う**
（slideshow.py の `_collect_preloaded` と同じ境界）。
"""

import logging
import queue
import threading
from typing import TYPE_CHECKING, Any

import pygame as pg

from src import i18n
from src.gui.renderer import TAP_DOWN, TAP_MOVE, TAP_UP
from src.gui.screens import ACTION_BACK, ACTION_RELOAD
from src.gui.text import Text, wrap_lines
from src.gui.widgets import (
    COLOR_BORDER,
    COLOR_PANEL,
    MIN_HIT_SIZE,
    UI_FONT_SIZE,
    UI_LABEL_FONT_SIZE,
    Button,
    Label,
    ScrollView,
    Widget,
)
from src.i18n import t

if TYPE_CHECKING:
    from src.config_manager import ConfigManager
    from src.gui.renderer import Renderer
    from src.immich_api import ImmichAPI
    from src.photo_cache import PhotoCache

logger = logging.getLogger(__name__)

HEADER_HEIGHT = 80
PAD = 10
CONTENT_PAD = 24
BACK_BUTTON_WIDTH = 120
DECIDE_BUTTON_WIDTH = 140

GRID_COLUMNS = 4
GRID_GAP = 16
ROW_GAP = 16

# アルバム名の折り返し（プラン delegated-leaping-perlis.md ステップ2でユーザーが決めた仕様）
ALBUM_NAME_MAX_LINES = 2

# サムネイルを保持する範囲（可視範囲の前後に何行ぶん余分に持つか）。
# 1枚 333x250x4 ≒ 333KB と大きいため狭く保つ。実測でピーク24枚。
THUMBNAIL_MARGIN_ROWS = 1
# アルバム名テクスチャを保持する範囲。**サムネイルより広く取る（ヒステリシス）。**
# 同じ1行で解放すると、行が可視範囲へ出入りするたびに作り直しになり、
# 実機（階層3・600件）で 45fps 未満のフレームが 6.9% -> 9.4% へ増えた。
# 3行に緩めると常駐は最大でも 4列 x 8行 x 2本 = 64本程度で、件数に依存しない
# 性質は保ったまま再生成の頻度が約 1/3 になる。文字テクスチャはサムネイルより
# はるかに小さいため、この程度の余分は許容できる。
LABEL_MARGIN_ROWS = 3
CAPTION_H_PAD = 8

# 選択状態を示す緑の枠線
COLOR_SELECTED = (90, 220, 110)
# サムネイルの下に重ねるキャプション帯（半透明の黒帯。参照元 photo-frame の
# AlbumWidget のタイトルラベル背景と同じ考え方）
COLOR_CAPTION_BG = (0, 0, 0)

# 仮想エントリ（実在のアルバムではない特別な選択肢）。サムネイルを持たないため
# 常に名前だけを中央に描く。
#
# **`albumName` を直接持たせず `name_key` で翻訳キーを持たせる。** モジュール
# 読み込み時に t() で解決して literal を埋めると、言語を切り替えても
# `_entries` に入ったままの古い言語の文字列が残ってしまう。表示名は
# `_AlbumCell._ensure_wrapped()` が `name_key` を見て毎回 t() で解決する。
# Immich から来る実アルバムの名前は `name_key` を持たないので、この経路の
# 対象外のまま（元データなので翻訳しない）。
_VIRTUAL_ENTRIES: list[dict[str, Any]] = [
    {'id': 'favorites', 'name_key': 'album.favorites', 'albumThumbnailAssetId': None,
     'is_virtual': True},
    {'id': 'daily_pickup', 'name_key': 'album.daily_pickup', 'albumThumbnailAssetId': None,
     'is_virtual': True},
]


class _AlbumCell(Widget):
    """
    グリッドの1マス。

    テクスチャの所有権は `AlbumScreen` が持ち、可視範囲の判定に応じて
    `texture` 属性を差し替える（このクラス自身は読み込み・解放のタイミングを
    知らない。責務を分離するため）。
    """

    def __init__(self, renderer: 'Renderer', rect: pg.Rect, entry: dict[str, Any],
                 index: int) -> None:
        super().__init__(rect)
        self._r = renderer
        self.entry = entry
        self.index = index
        self.texture = None
        self.selected = False
        self._pressed = False
        # 折り返し後の各行の**文字列**（最大 ALBUM_NAME_MAX_LINES 個）。
        # アルバム名は画面が開いている間変化せず、幅の計測もフォント依存で
        # SDL の世代に左右されないため、セルの生存期間に一度だけ計算して保持する
        # （毎フレーム二分探索を回さないため）。数百バイトなので件数に比例しても問題ない。
        self._wrapped: list[str] | None = None
        # 上の文字列から作ったテクスチャ。**こちらは可視範囲を出たら手放す**
        # （release_labels()）。保持し続けるとアルバム数に比例して積み上がる。
        self._label_lines: list[Text] = []
        self._caption_height = 0

    def handle_down(self, x: int, y: int) -> bool:
        if not self.visible or not self.rect.collidepoint(x, y):
            return False
        self._pressed = True
        return True

    def handle_up(self, x: int, y: int) -> int | None:
        """ 戻り値は選択された自分の index。押されていなければ None """
        fired = self._pressed and self.visible and self.rect.collidepoint(x, y)
        self._pressed = False
        return self.index if fired else None

    def handle_cancel(self) -> None:
        # ScrollView がスクロールへ切り替わると、DOWN を受理した後でも
        # handle_up が呼ばれずに終わる。縦スクロールを始めるつもりでセルに
        # 触れた場合、これが無いと押下ハイライトが残ったままになる。
        self._pressed = False

    def _ensure_wrapped(self) -> None:
        """
        アルバム名の折り返しとラベルのテクスチャを用意する。

        **折り返し（`wrap_lines()` の二分探索）はセルの生存期間に一度だけ行う。**
        アルバム名はこの画面が開いている間変化せず、幅の計測は
        `Renderer.font()`（SDL の資源を持たないため世代をまたいで生き残る）に
        依存するだけなので、SDL が再生成されても結果は変わらない。

        テクスチャの方は `release_labels()` で手放されうるので、無ければここで
        作り直す。`Text.set()` は内容・サイズ・色・世代がすべて同じなら何もしないため、
        `draw()` から毎フレーム呼んでも再生成は起きない
        （SDL 再生成後の作り直しもこの `set()` が拾う）。
        """
        # UI_LABEL_FONT_SIZE / CAPTION_H_PAD は論理px。wrap_lines() は
        # Renderer.font() を直接使う（物理pxを受け取る契約）ため、ここで変換する。
        # 折り返しの幅計測と実際の描画で同じ物理pxを使わないと行崩れするので、
        # 変換した値を _label_font_size として以降でも使い回す。
        label_font_size = self._r.px(UI_LABEL_FONT_SIZE)
        caption_h_pad = self._r.px(CAPTION_H_PAD)
        if self._wrapped is None:
            name_key = self.entry.get('name_key')
            if name_key:
                # 仮想エントリ（お気に入り／デイリーピックアップ）。表示名は
                # 現在言語で t() から作る。Immich から来る実アルバムの名前
                # （albumName）は元データなので絶対に翻訳しない
                name = t(name_key)
            else:
                name = str(self.entry.get('albumName') or self.entry.get('id') or '')
            max_width = max(1, self.rect.width - caption_h_pad * 2)
            self._wrapped = wrap_lines(self._r, name, label_font_size, max_width,
                                       ALBUM_NAME_MAX_LINES)

        if len(self._label_lines) != len(self._wrapped):
            self._label_lines = [Text(self._r) for _ in self._wrapped]
        for text, line in zip(self._label_lines, self._wrapped):
            text.set(line, label_font_size)

        line_height = max((t.height for t in self._label_lines), default=0)
        total_h = line_height * len(self._label_lines)
        self._caption_height = total_h + caption_h_pad * 2 if total_h else 0

    def release_labels(self) -> None:
        """
        アルバム名のテクスチャだけを手放す（折り返し結果の文字列は残す）。

        可視範囲外のセルがテクスチャを持ち続けると**アルバム数に比例して
        積み上がる**（600件で1,177本を実機で実測した）。サムネイルと同じ範囲判定で
        解放し、再入時は `_ensure_wrapped()` がテクスチャだけ作り直す
        （文字列は残っているので二分探索は走らない）。
        """
        self._label_lines = []
        self._caption_height = 0

    def invalidate_wrapped(self) -> None:
        """
        折り返しキャッシュ（**文字列側**の `_wrapped`）を破棄する。

        `_ensure_wrapped()` は `self._wrapped is None` のときだけ名前を作り直すため、
        言語が変わってもこれを呼ばないと仮想エントリの表示名（`name_key` から
        `t()` で作る）が古い言語のまま `_wrapped` に残り続ける。
        `release_labels()` はテクスチャ側だけを手放す既存の契約なので、
        文字列側を破棄するこちらは別のメソッドにしてある。
        """
        self._wrapped = None

    def _draw_label_lines(self, center_x: int, top_y: int) -> None:
        y = top_y
        for text in self._label_lines:
            text.draw_centered(center_x, y)
            y += text.height

    def draw(self) -> None:
        if not self.visible:
            return
        self._r.fill_rect(self.rect, COLOR_PANEL, alpha=200)
        self._ensure_wrapped()

        if self.texture is not None:
            self._draw_thumbnail_cover()
            caption_h = min(self.rect.height, self._caption_height)
            caption = pg.Rect(self.rect.x, self.rect.bottom - caption_h,
                              self.rect.width, caption_h)
            self._r.fill_rect(caption, COLOR_CAPTION_BG, alpha=160)
            total_h = sum(t.height for t in self._label_lines)
            self._draw_label_lines(self.rect.centerx,
                                   caption.y + (caption_h - total_h) // 2)
        else:
            # サムネイルの無いアルバムと仮想エントリは名前だけを中央に描く
            total_h = sum(t.height for t in self._label_lines)
            self._draw_label_lines(self.rect.centerx, self.rect.centery - total_h // 2)

        if self.selected:
            self._r.draw_rect(self.rect, COLOR_SELECTED, alpha=255, width=self._r.px(4))
        else:
            self._r.draw_rect(self.rect, COLOR_BORDER, alpha=120, width=self._r.px(1))

    def _draw_thumbnail_cover(self) -> None:
        """
        サムネイルを中央クロップしてセルの縦横比に合わせて描く（変形させない）。

        `Texture.draw()` の `srcrect` はテクスチャ側の切り出し矩形、`dstrect` は
        描画先の矩形。両方 GPU 側のスケーリング・クロップで完結し、CPU で
        ピクセルを書き換えないため禁止パターン（描画パスでのリサイズ）には
        当たらない（`dstrect` だけを指定していた元の実装と同じ扱い）。
        """
        tex = self.texture
        tex_w, tex_h = tex.width, tex.height
        if tex_w <= 0 or tex_h <= 0:
            tex.draw(dstrect=self.rect)
            return

        cell_w, cell_h = self.rect.width, self.rect.height
        # セルの縦横比に合わせた cover 計算。現在セルは正方形(232x232) だが、
        # レイアウト定数を変えても歪まないよう比率で計算する（正方形決め打ちにしない）。
        src_aspect = tex_w / tex_h
        dst_aspect = cell_w / cell_h
        if src_aspect > dst_aspect:
            # テクスチャの方が横長 -> 左右の余りを均等に落とす
            crop_h = tex_h
            crop_w = max(1, min(tex_w, round(tex_h * dst_aspect)))
        else:
            # テクスチャの方が縦長（または同比率） -> 上下の余りを均等に落とす
            crop_w = tex_w
            crop_h = max(1, min(tex_h, round(tex_w / dst_aspect)))
        src_x = (tex_w - crop_w) // 2
        src_y = (tex_h - crop_h) // 2
        srcrect = pg.Rect(src_x, src_y, crop_w, crop_h)
        tex.draw(srcrect=srcrect, dstrect=self.rect)


class AlbumScreen:
    """
    アルバム選択画面。

    `on_enter()` でアルバム一覧をワーカースレッドから取得し、先頭に仮想エントリ
    （お気に入り／デイリーピックアップ）を差し込んだうえでグリッドを組む。
    「決定」を押すと `source`/`album_id`/`album_name` を書き込んで `ACTION_RELOAD`
    を返す（画面を閉じて写真リストを再取得させるのは main.py 側の責務）。
    """

    def __init__(self, renderer: 'Renderer', config: 'ConfigManager',
                 api: 'ImmichAPI', cache: 'PhotoCache') -> None:
        self._r = renderer
        self._config = config
        self._api = api
        self._cache = cache
        self._gen = (-1, -1)

        self._back_button: Button | None = None
        self._decide_button: Button | None = None
        self._title: Label | None = None
        self._loading_label: Label | None = None
        self._scroll: ScrollView | None = None

        self._entries: list[dict[str, Any]] = []
        self._cells: list[_AlbumCell] = []
        self._selected_index: int | None = None
        self._loading = False
        self._cell_size = 0
        self._row_height = 0

        # 常駐テクスチャは index -> Texture。可視範囲外になったら pop して解放する
        self._textures: dict[int, Any] = {}
        # 二重にリクエストを積まないための集合。範囲外に出たら discard し、
        # 再度可視範囲に入ったときに取り直せるようにする
        self._requested: set[int] = set()
        # 前フレームでラベルのテクスチャを許可していた index の範囲 [start, end)。
        # **アルバム名のテクスチャを解放する対象を求めるために持つ。**
        # サムネイルと違い「要求した index の集合」が無い（描画時に遅延生成される）ため、
        # 全セルを毎フレーム走査せずに済ませるには前回の範囲を覚えておく必要がある。
        # 範囲は連続して動くので、差分だけを解放すれば取りこぼさない。
        self._label_range: tuple[int, int] = (0, 0)

        self._list_queue: queue.Queue = queue.Queue()
        self._thumb_request_queue: queue.Queue = queue.Queue()
        self._thumb_result_queue: queue.Queue = queue.Queue()
        # **訪問（on_enter〜on_leave）ごとの世代番号。** on_leave() の join はタイムアウト
        # 2秒だが、ワーカーが呼ぶ download_asset()/内部の _get() は最大30秒
        # （immich_api.py の DEFAULT_TIMEOUT）かかりうる。低速な回線や実機で常時稼働する
        # CPU 負荷の高い常駐コンテナによる CPU 飽和のもとでは2秒で終わる保証が無いため、
        # join 後もワーカーが生き残ることを前提に設計する。
        #
        # 対策は main.py の `_list_request_token`（写真リスト取得）と同じ考え方で、
        # 結果に訪問世代を載せ、取り込み側が現在の世代と一致するものだけを採用する。
        # 加えて、**stop_event も on_enter() のたびに新しいオブジェクトを作って
        # ワーカーへ渡す**（self._stop_event を使い回して clear()/set() を繰り返すと、
        # 古い訪問のワーカーが「自分の目印だと思っていた Event」を新しい訪問が
        # clear() してしまい、古いワーカーの is_set() が False に戻ってしまう
        # 事故が起きる。これがレビューで指摘された逸脱点そのもの）。
        # 世代フィルタと個別 Event の両方を備えることで、古いワーカーが多少長生きしても
        # 実害（誤った index への貼り付け・キューの取りこぼし）が出ないようにしている。
        self._session = 0
        self._stop_event: threading.Event | None = None
        self._threads: list[threading.Thread] = []

        self._build_header()

    # ------------------------------------------------------------------ 構築

    def _build_header(self) -> None:
        """ ヘッダ（戻る／タイトル／決定）だけを組み立てる。グリッドは entries が揃ってから """
        width, _height = self._r.size
        px = self._r.px
        header_height = px(HEADER_HEIGHT)
        pad = px(PAD)
        back_button_width = px(BACK_BUTTON_WIDTH)
        decide_button_width = px(DECIDE_BUTTON_WIDTH)

        self._back_button = Button(
            self._r, pg.Rect(pad, pad, back_button_width, header_height - pad * 2),
            t('common.back'), action=ACTION_BACK, font_size=UI_FONT_SIZE)
        self._decide_button = Button(
            self._r,
            pg.Rect(width - pad - decide_button_width, pad,
                   decide_button_width, header_height - pad * 2),
            t('album.confirm'), font_size=UI_FONT_SIZE)
        self._title = Label(
            self._r, pg.Rect(0, pad, width, header_height - pad * 2),
            t('album.title'), font_size=UI_FONT_SIZE + 8, centered=True)
        self._loading_label = Label(
            self._r, pg.Rect(0, header_height, width, self._r.size[1] - header_height),
            t('album.loading'), font_size=UI_FONT_SIZE, centered=True)
        # SDL の世代と言語の世代の組で持つ（i18n.generation ↔ 3画面の
        # _recreate_if_needed の対。.claude/architecture.md「対で更新が必要な箇所」参照）。
        self._gen = (self._r.generation, i18n.generation())

    def _build_grid(self) -> None:
        """ `self._entries` が揃った後にグリッドを組む（ネットワーク I/O は済んでいる前提） """
        width, height = self._r.size
        px = self._r.px
        header_height = px(HEADER_HEIGHT)
        content_pad = px(CONTENT_PAD)
        grid_gap = px(GRID_GAP)
        row_gap = px(ROW_GAP)
        min_hit_size = px(MIN_HIT_SIZE)

        viewport = pg.Rect(0, header_height, width, height - header_height)
        self._scroll = ScrollView(self._r, viewport)

        content_width = max(1, width - content_pad * 2)
        cell_w = max(min_hit_size, (content_width - grid_gap * (GRID_COLUMNS - 1)) // GRID_COLUMNS)
        self._cell_size = cell_w
        self._row_height = cell_w + row_gap

        self._cells = []
        for index, entry in enumerate(self._entries):
            row, col = divmod(index, GRID_COLUMNS)
            x = content_pad + col * (cell_w + grid_gap)
            y = row * self._row_height
            cell = _AlbumCell(self._r, pg.Rect(x, y, cell_w, cell_w), entry, index)
            self._cells.append(cell)
            self._scroll.add(cell)

        self._restore_selection()

    def _restore_selection(self) -> None:
        """ 画面を開いた時点の選択状態を現在の設定（source/album_id）から復元する """
        source = self._config.get('source')
        album_id = self._config.get('album_id')

        idx = 0  # 既定は先頭（お気に入り）
        if source == 'daily_pickup':
            idx = 1
        elif source == 'album' and album_id:
            for i, entry in enumerate(self._entries):
                if not entry.get('is_virtual') and entry.get('id') == album_id:
                    idx = i
                    break
        self._select(idx)

    def _select(self, index: int) -> None:
        if not (0 <= index < len(self._cells)):
            return
        for cell in self._cells:
            cell.selected = False
        self._cells[index].selected = True
        self._selected_index = index

    # ------------------------------------------------------------------ 契約

    def on_enter(self) -> None:
        # 訪問世代を進め、この訪問専用の Event を作る（__init__ 側のコメント参照）。
        # ワーカーには session/stop_event を引数で渡し、self._session /
        # self._stop_event を動的に読み直させない。読み直すと、次の on_enter() が
        # 差し替えた「新しい」値を古いスレッドが拾ってしまい、世代フィルタの意味が
        # なくなるため。
        self._session += 1
        session = self._session
        stop_event = threading.Event()
        self._stop_event = stop_event

        self._entries = []
        self._cells = []
        self._scroll = None
        self._selected_index = None
        self._textures.clear()
        self._requested.clear()
        self._label_range = (0, 0)
        self._drain(self._list_queue)
        self._drain(self._thumb_request_queue)
        self._drain(self._thumb_result_queue)
        self._loading = True
        # ロード中は「決定」を押せなくする（必須2の対応）。グリッドが揃うまで
        # 選択も無いため、押せても無意味な ACTION_RELOAD を返すだけになる。
        if self._decide_button is not None:
            self._decide_button.visible = False

        list_thread = threading.Thread(target=self._fetch_albums_worker,
                                       args=(session, stop_event),
                                       name=f'album-fetch-{session}', daemon=True)
        self._threads.append(list_thread)
        list_thread.start()

        thumb_thread = threading.Thread(target=self._thumb_worker,
                                        args=(session, stop_event),
                                        name=f'album-thumb-{session}', daemon=True)
        self._threads.append(thumb_thread)
        thumb_thread.start()

    def on_leave(self) -> None:
        """
        ワーカーへ停止を伝え、保持しているテクスチャの参照をすべて落とす。

        **join がタイムアウトしてもワーカーが完全に止まった保証にはならない。**
        それでも安全なのは、この訪問の stop_event はもうどこからも clear() されず
        （次の on_enter() は新しい Event を作る）、かつ結果は世代番号で
        フィルタされるため、遅れて届いた結果は _collect_* 側で黙って捨てられるから。
        """
        if self._stop_event is not None:
            self._stop_event.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()
        self._textures.clear()
        self._requested.clear()
        self._label_range = (0, 0)
        for cell in self._cells:
            cell.texture = None
            # 画面を閉じている間はアルバム名のテクスチャも持たない。
            # 次に開くときは on_enter() がセルごと作り直す
            cell.release_labels()

    def update(self, now: float) -> None:
        self._recreate_if_needed()
        self._collect_album_list()
        if self._loading or self._scroll is None:
            return
        self._collect_thumbnails()
        self._sync_visible_cells()
        self._scroll.update(now)

    def draw(self) -> None:
        self._recreate_if_needed()
        width, _height = self._r.size
        self._r.fill_rect(pg.Rect(0, 0, *self._r.size), (20, 20, 20), alpha=255)

        # スクロール内容（グリッド）を先に描き、ヘッダは最後に上書きする。
        # ScrollView は可視行だけ描く方式のため行の一部が viewport の上端を
        # はみ出すことがあり、後から不透明なヘッダ帯を敷いて隠す
        # （.claude/plans/delegated-leaping-perlis.md 修正1）。
        if self._loading or self._scroll is None:
            self._loading_label.draw()
        else:
            self._scroll.draw()

        # ヘッダ帯。背景全体と同じ色で不透明に塗ってから、その上にボタン等を描く
        # （帯が不透明でないと下のグリッドが透けて見える）。
        self._r.fill_rect(pg.Rect(0, 0, width, self._r.px(HEADER_HEIGHT)), (20, 20, 20), alpha=255)
        self._back_button.draw()
        self._title.draw()
        self._decide_button.draw()

    def handle_input(self, kind: str, x: int, y: int) -> str | None:
        self._recreate_if_needed()

        if kind == TAP_DOWN:
            self._back_button.handle_down(x, y)
            self._decide_button.handle_down(x, y)
            if not self._loading and self._scroll is not None:
                self._scroll.handle_down(x, y)
            return None

        if kind == TAP_MOVE:
            if not self._loading and self._scroll is not None:
                self._scroll.handle_move(x, y)
            return None

        if kind == TAP_UP:
            if self._back_button.handle_up(x, y):
                return ACTION_BACK
            if self._decide_button.handle_up(x, y):
                # ロード中・未選択のときは何も確定しない。ACTION_RELOAD を無条件で
                # 返すと、main.py が画面を閉じて不要な再読み込みを走らせてしまう
                # （必須2の対応）。
                if self._confirm_selection():
                    return ACTION_RELOAD
                return None
            if not self._loading and self._scroll is not None:
                result = self._scroll.handle_up(x, y)
                if isinstance(result, int):
                    self._select(result)
            return None

        return None

    # ------------------------------------------------------------ 決定・保存

    def _confirm_selection(self) -> bool:
        """
        選択されているエントリを source/album_id/album_name として保存する。

        戻り値は「実際に確定できたか」。呼び出し側（handle_input）はこれを見て
        ACTION_RELOAD を返すかどうかを決める。ロード中や未選択のまま「決定」が
        押された場合は False を返し、何も書き込まない（必須2の対応）。
        """
        if self._loading or self._selected_index is None or not (
                0 <= self._selected_index < len(self._entries)):
            logger.info('アルバムが選択されていない、または読み込み中のため決定を無視しました')
            return False

        entry = self._entries[self._selected_index]
        entry_id = entry.get('id')
        if entry_id == 'favorites':
            self._config.set('source', 'favorites')
            self._config.set('album_id', '')
            self._config.set('album_name', '')
        elif entry_id == 'daily_pickup':
            self._config.set('source', 'daily_pickup')
            self._config.set('album_id', '')
            self._config.set('album_name', '')
        else:
            self._config.set('source', 'album')
            self._config.set('album_id', entry_id)
            self._config.set('album_name', entry.get('albumName') or '')
        logger.info('写真ソースを変更しました: source=%s album_id=%s album_name=%s',
                    self._config.get('source'), self._config.get('album_id'),
                    self._config.get('album_name'))
        return True

    # ------------------------------------------------------------ アルバム取得

    def _fetch_albums_worker(self, session: int, stop_event: threading.Event) -> None:
        """
        ワーカースレッド。ネットワーク I/O のみ行い、SDL には一切触れない。

        `session`/`stop_event` はこのスレッドが起動された時点の on_enter() から
        引数で受け取ったもので、以降 `self._session`/`self._stop_event` を
        読み直さない（それらは次の on_enter() で別の値に差し替わりうるため。
        __init__ のコメント参照）。

        `ImmichAPI.fetch_albums()` は通信エラーを内部で握って空リストを返す
        契約になっている（0件は「壊れている」とは限らない、が正常系の切り分けが
        できない）。そのため、**通信が失敗した可能性がある空リストのときは
        古いサムネイルキャッシュを消さない**（本来のアルバムがまだ存在するのに、
        一時的な通信エラーで「存在しない」と誤判定してキャッシュを全消しする
        事故を避けるための保守的な判断）。
        """
        if stop_event.is_set():
            return
        albums = self._api.fetch_albums()
        if stop_event.is_set():
            return

        if albums:
            self._cache.cleanup_thumbnails(albums)

        entries = list(_VIRTUAL_ENTRIES) + albums
        if not stop_event.is_set():
            self._list_queue.put((session, entries))

    def _collect_album_list(self) -> None:
        if not self._loading:
            return
        try:
            session, entries = self._list_queue.get_nowait()
        except queue.Empty:
            return
        if session != self._session:
            # 既に on_leave() で見切りをつけた古い訪問の結果。破棄する
            # （join タイムアウト後もワーカーが生きていた場合にここへ来る）
            logger.info('アルバム一覧の取得結果を破棄しました（古い訪問 session=%d, 現在=%d）',
                        session, self._session)
            return
        self._entries = entries
        self._build_grid()
        self._loading = False
        if self._decide_button is not None:
            self._decide_button.visible = True
        logger.info('アルバム一覧を取得しました: %d 件（仮想エントリ含む）', len(entries))

    # ------------------------------------------------------------ サムネイル

    def _thumb_worker(self, session: int, stop_event: threading.Event) -> None:
        """
        永続ワーカースレッド。可視範囲が変わるたびにメインスレッドが積む
        リクエストを1件ずつ処理する（同時ダウンロード数を絞り、2.4GHz Wi-Fi の
        帯域を使い切らないため）。

        `session`/`stop_event` の扱いは `_fetch_albums_worker` と同じ
        （引数で固定し、動的に読み直さない）。
        """
        while not stop_event.is_set():
            try:
                req_session, index, album_id, thumb_id = self._thumb_request_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            path = self._cache.get_thumbnail_path(album_id, thumb_id)
            if path is None:
                raw = self._api.download_asset(thumb_id, size='thumbnail')
                if raw is not None:
                    path = self._cache.store_thumbnail(album_id, thumb_id, raw)

            if not stop_event.is_set():
                self._thumb_result_queue.put((req_session, index, path))

    def _collect_thumbnails(self) -> None:
        """ ワーカーの結果を取り込んでテクスチャ化する（メインスレッド限定） """
        while True:
            try:
                session, index, path = self._thumb_result_queue.get_nowait()
            except queue.Empty:
                return

            if session != self._session:
                # 古い訪問のワーカーが join タイムアウト後に届けた結果。
                # 現在のエントリ配列と index の対応が保証されないため必ず捨てる
                # （これを怠ると誤った index にサムネイルが貼られる事故になる）。
                continue
            if not (0 <= index < len(self._cells)):
                continue
            if index not in self._requested:
                # 取得中に範囲外へ出て破棄済み。今さら足しても表示されないので捨てる
                continue
            if path is None:
                logger.warning('アルバムサムネイルを取得できませんでした: index=%d', index)
                continue

            texture = self._r.texture_from_image(path)
            if texture is None:
                continue
            self._textures[index] = texture
            self._cells[index].texture = texture

    def _visible_index_range(self, margin_rows: int = THUMBNAIL_MARGIN_ROWS) -> tuple[int, int]:
        """
        可視範囲＋前後 `margin_rows` 行を index の範囲 [start, end) で返す。

        サムネイル（狭い）とアルバム名（広い）で違う範囲を使うため引数にしてある。
        """
        if self._scroll is None or self._row_height <= 0:
            return 0, 0
        top, bottom = self._scroll.visible_range()
        row_start = max(0, top // self._row_height - margin_rows)
        row_end = bottom // self._row_height + margin_rows
        start = int(row_start) * GRID_COLUMNS
        end = (int(row_end) + 1) * GRID_COLUMNS
        return max(0, start), min(len(self._entries), end)

    def _sync_visible_cells(self) -> None:
        """
        可視範囲の周辺だけテクスチャを持つよう同期する。

        対象は**サムネイルとアルバム名の両方**で、保持する範囲は別々である
        （サムネイルは前後 THUMBNAIL_MARGIN_ROWS 行、アルバム名は前後
        LABEL_MARGIN_ROWS 行。定数のコメントに理由を書いてある）。

        - 範囲に入った・サムネイルを持つ・まだ要求していないエントリはキューに積む
        - 範囲外に出たエントリはテクスチャの参照を落とし（GC 対象にし）、
          要求済み集合からも外す（戻ってきたときにディスクキャッシュから
          素早く再取得できる。ファイル自体は削除していないため通信は発生しない）
        - 範囲外に出たセルのアルバム名テクスチャも `release_labels()` で手放す。
          **これが無いと一度でも描かれたセルの分が全て残り、アルバム数に比例して
          積み上がる**（600件で1,177本を実機で実測した）
        """
        start, end = self._visible_index_range()

        for index in range(start, end):
            if index in self._requested:
                continue
            entry = self._entries[index]
            if entry.get('is_virtual') or not entry.get('albumThumbnailAssetId'):
                continue
            self._requested.add(index)
            self._thumb_request_queue.put(
                (self._session, index, entry['id'], entry['albumThumbnailAssetId']))

        for index in list(self._requested):
            if start <= index < end:
                continue
            self._requested.discard(index)
            texture = self._textures.pop(index, None)
            if texture is not None and 0 <= index < len(self._cells):
                self._cells[index].texture = None

        # ラベルは「前回の範囲のうち今回の範囲から外れたもの」だけを解放する。
        # 全セルを走査しないのは、件数に比例するループを毎フレーム増やさないため。
        # **判定に使うのはサムネイルより広い範囲**（LABEL_MARGIN_ROWS）。
        # ラベルが作られるのは可視範囲の中だけなので、広い範囲を覚えておけば
        # 取りこぼしは起きない（狭い範囲 ⊂ 広い範囲）。
        label_start, label_end = self._visible_index_range(LABEL_MARGIN_ROWS)
        prev_start, prev_end = self._label_range
        for index in range(prev_start, min(prev_end, len(self._cells))):
            if label_start <= index < label_end:
                continue
            self._cells[index].release_labels()
        self._label_range = (label_start, label_end)

    # ------------------------------------------------------- SDL 再生成への追従

    def _recreate_if_needed(self) -> None:
        """
        消灯復帰で SDL が作り直されると保持中のテクスチャは全て無効になる。
        言語が変わった場合はそれに加えてヘッダの文言と、各セルの折り返し
        キャッシュ（`_AlbumCell._wrapped`。仮想エントリの表示名の元）も
        作り直す必要がある。

        **SDL の世代の変化と言語の世代の変化で無効になるものが違う**ため、
        2つを分けて判定する（.claude/architecture.md「対で更新が必要な箇所」参照）。
        どちらか片方だけの変化で、もう一方の処理まで行わないこと
        （言語だけを変えたのにサムネイルを全部破棄して読み直す、という
        取りこぼしを過去に実装してレビューで指摘された）。

        - **SDL のみ**: `Renderer.texture_from_image()` で作った
          `self._textures` / `cell.texture`（サムネイル）と、セルが持つ
          アルバム名のテクスチャ（`cell.release_labels()`）が無効になる。
          `Text` 自体は `set()` が世代を見て自動的に作り直すので
          `release_labels()` は必須ではないが（無効なテクスチャを保持し続けない
          方針を揃えるため呼ぶ）、**サムネイルの `Texture` にはその安全弁が無い**
          （`Renderer.texture_from_image()` が返す生の `Texture` で、世代チェックは
          呼び出し側の責務）。ヘッダの `Button`/`Label` は内部の `Text` が
          `set()` のたびに世代を見て自動的に作り直すため `_build_header()` を
          呼び直す必要は無い。折り返し文字列（`_wrapped`）も世代に依存しないので残る
        - **言語のみ**: サムネイルは言語と無関係なので**破棄しない**
          （`self._textures` / `cell.texture` はそのまま）。`_build_header()` を
          呼び直し（ヘッダの `t()` 呼び出し自体を起こし直さないと文言が新しい
          言語に差し替わらない）、各セルの `_wrapped` を `invalidate_wrapped()` で
          破棄する。**`_ensure_wrapped()` は `self._wrapped is None` のときだけ
          名前を作り直すため、これを破棄しないと仮想エントリの表示名が古い言語の
          まま `_wrapped` に残り続ける。** アルバム名のテクスチャ自体は
          `release_labels()` を呼ばなくても、`_ensure_wrapped()` が新しい文字列で
          `Text.set()` を呼べば内容の違いを見て自動的に作り直される
          （`Text.set()` は世代だけでなく内容の一致も見るため）。実アルバムの
          `_wrapped` も一緒に破棄されるが、`albumName` 自体は言語に依存しないため
          中身は変わらない（再計算の手間が増えるだけ）。`_build_header()` は
          `_decide_button` を既定値（`visible=True`）で作り直すため、**ロード中
          （`self._loading`）なら `visible=False` を再適用する**（`on_enter()` が
          押せなくしていた状態を壊さないため。必須2の対応）

        どちらの場合も**アルバム一覧の再取得（ネットワーク I/O）は絶対に行わない**
        （済んでいる I/O をやり直す理由が無い。`on_enter()` のワーカー起動経路には
        触れない）。グリッドのレイアウトと選択状態も変えない。
        """
        current_gen = (self._r.generation, i18n.generation())
        if self._gen == current_gen:
            return
        first_time = self._gen[0] < 0
        prev_gen = self._gen
        self._gen = current_gen
        if first_time:
            return

        sdl_changed = prev_gen[0] != current_gen[0]
        lang_changed = prev_gen[1] != current_gen[1]

        if sdl_changed:
            self._textures.clear()
            self._requested.clear()
            self._label_range = (0, 0)
            for cell in self._cells:
                cell.texture = None
                cell.release_labels()

        if lang_changed:
            for cell in self._cells:
                cell.invalidate_wrapped()
            self._build_header()
            # _build_header() は _decide_button を既定値(visible=True)で
            # 作り直すため、ロード中に「決定」を押せなくしていた状態
            # （on_enter() 参照）を再適用する。忘れると、言語を変えてから
            # 開いたアルバム画面はロード中でも「決定」が押せる見た目に戻る
            # （_confirm_selection() の self._loading ガードがあるため
            # 実害は無いが、必須2の設計が崩れる）。
            if self._decide_button is not None:
                self._decide_button.visible = not self._loading

        logger.info(
            'SDL/言語の変化にあわせてアルバム画面を更新しました（gen=%r, sdl_changed=%s, lang_changed=%s）',
            self._gen, sdl_changed, lang_changed)

    # ------------------------------------------------------------------ 補助

    @staticmethod
    def _drain(q: 'queue.Queue') -> None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return
