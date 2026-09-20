"""
スライドショーの遷移効果

**常駐テクスチャは増やさない。** ここに書く各遷移は `slideshow.py` が持つ
current / next の2枚のテクスチャだけを使い、`Renderer.fill_rect` による
GPU 側の単色塗りで合成する（禁止パターン: CPU 側でのフルスクリーン合成）。
拡縮や回転を伴う遷移は、描画パスでのリサイズ禁止（.claude/architecture.md）に
抵触するうえ、既定のニアレスト補間では画質も落ちるため採らない。ここでは
配置・アルファ・単色塗り・等倍の切り出しだけで表現できるものに絞っている。

遷移の種類の決定は `_begin_fade()` の時点で1回だけ行い、フェード中は固定する
（`slideshow.py` 側の契約）。`random` は直前に使った具体的な種類を避けて
4種から選び直す。設定値が不正な場合は `crossfade` にフォールバックし、
同じ不正値を何度渡されても警告ログは1回だけにする（毎フレーム呼ばれる
描画経路ではないが、設定ファイルを手で壊した場合にログが埋まるのを防ぐため）。
"""

import logging
import random as random_module
from typing import Any, Callable, Protocol

import pygame as pg

logger = logging.getLogger(__name__)

# 設定キー `transition` が取りうる値
CROSSFADE = 'crossfade'
FADE_BLACK = 'fade_black'
SLIDE = 'slide'
WIPE = 'wipe'
RANDOM = 'random'

# 実際に描画できる具体的な遷移（random の抽選対象。random 自身は含まない）
CONCRETE_KINDS: tuple[str, ...] = (CROSSFADE, FADE_BLACK, SLIDE, WIPE)

# 設定画面のスピナーに出す全選択肢
ALL_VALUES: tuple[str, ...] = CONCRETE_KINDS + (RANDOM,)


class _RandomLike(Protocol):
    def choice(self, seq: list[str]) -> str: ...


def _ease(t: float) -> float:
    """
    smoothstep によるイーズイン・イーズアウト。

    slide / wipe は等速で動くと機械的な見た目になるため、始点・終点付近を
    緩めてある。t はあらかじめ 0.0〜1.0 にクランプされている前提。
    """
    return t * t * (3.0 - 2.0 * t)


def _clamp01(t: float) -> float:
    return max(0.0, min(1.0, t))


def _alpha_from_ratio(ratio: float) -> int:
    """
    0.0〜1.0 の比率を 0〜255 の整数アルファへ変換する。

    `int()` による切り捨てで、四捨五入にしない。crossfade の見た目を
    従来の `_fade_alpha()`（`int(ratio * 255)`）と完全に一致させるため
    （四捨五入へ変えると1フレームぶん明るさがずれる）。
    """
    return max(0, min(255, int(max(0.0, min(1.0, ratio)) * 255)))


def pick_random(previous: str | None, rng: _RandomLike = random_module) -> str:
    """
    `CONCRETE_KINDS` から1つ選ぶ。`previous` と同じ種類は候補から除く。

    候補が1つしか残らない状況は `CONCRETE_KINDS` が4種ある限り起きない
    （常に3種以上残る）。`rng` はヘッドレステストで差し替えられるよう
    引数で受け取る（既定は標準ライブラリの `random` モジュール）。
    """
    candidates = [k for k in CONCRETE_KINDS if k != previous]
    if not candidates:  # pragma: no cover - CONCRETE_KINDS が2種未満にならない限り起きない
        candidates = list(CONCRETE_KINDS)
    return rng.choice(candidates)


def normalize_value(value: object, warned_values: set[str]) -> str:
    """
    設定値 `transition` を既知の値へ正規化する。未知の値は `crossfade` 扱いにする。

    `value` は `ConfigManager.get()` がそのまま返す `object`。設定ファイルを
    手で壊すと文字列以外（list / dict / None / 数値など）が来ることがあり、
    `value in ALL_VALUES` の前に `isinstance` で弾かないと、その後
    `warned_values`（`set[str]`）に非文字列を入れようとして
    `TypeError: unhashable type` で落ちる（list / dict はハッシュ不可）。
    自動送りのたびに `_begin_fade()` から呼ばれるため、落ちると次の自動送りで
    再び同じ設定値を読んで再クラッシュする＝実質のクラッシュループになる。

    `warned_values` は呼び出し側（`SlideshowScreen`）がインスタンスごとに
    保持する集合。同じ不正値を渡された場合に毎回 WARNING を出すと、
    設定ファイルを直接編集して壊した場合にログが埋まるため、値ごとに1回だけにする。
    非文字列はそのままでは `set` のキーにできない場合がある（list / dict）ため、
    `repr(value)` という常にハッシュ可能な文字列に変換してから記録する。
    """
    if isinstance(value, str) and value in ALL_VALUES:
        return value
    warn_key = value if isinstance(value, str) else repr(value)
    if warn_key not in warned_values:
        warned_values.add(warn_key)
        if isinstance(value, str):
            logger.warning('未知の transition 設定値です。crossfade として扱います: %r', value)
        else:
            logger.warning(
                'transition 設定値が文字列ではありません。crossfade として扱います: %r', value)
    return CROSSFADE


def resolve_transition(value: object, previous: str | None, warned_values: set[str],
                        rng: _RandomLike = random_module) -> str:
    """
    設定値から、このフェードで実際に使う具体的な遷移種類を1つ決める。

    `_begin_fade()` から1回だけ呼ばれる想定。`random` はここで具体的な
    種類へ確定し、フェード中はその値を使い続ける（毎フレーム抽選しない）。
    """
    kind = normalize_value(value, warned_values)
    if kind == RANDOM:
        return pick_random(previous, rng)
    return kind


DstRectFn = Callable[[Any], pg.Rect]


def draw(kind: str, renderer: Any, dst_rect: 'DstRectFn', current: Any, next_tex: Any,
         t: float) -> None:
    """
    フェード中の1フレームを描く。

    `kind` は `resolve_transition()` で確定済みの具体的な種類のみを受け取る想定
    （`random` はここには来ない）。想定外の値が来た場合も安全側として
    crossfade にフォールバックする。`t` は 0.0（フェード開始）〜1.0（完了）。
    """
    t = _clamp01(t)
    if kind == FADE_BLACK:
        _draw_fade_black(renderer, dst_rect, current, next_tex, t)
    elif kind == SLIDE:
        _draw_slide(renderer, dst_rect, current, next_tex, t)
    elif kind == WIPE:
        _draw_wipe(renderer, dst_rect, current, next_tex, t)
    else:
        _draw_crossfade(renderer, dst_rect, current, next_tex, t)


# ---------------------------------------------------------------- 各遷移の実装

def rect_difference(base: pg.Rect, cover: pg.Rect) -> list[pg.Rect]:
    """
    base のうち cover に覆われない領域を、最大4枚の矩形に分けて返す。

    上帯・下帯を先に全幅で切り出し、左右の帯は交差の高さに揃えることで
    互いに重ならないようにしている。**重複させてはいけない。**
    半透明で塗るため、同じ画素を2度塗るとそこだけ濃くなってしまう。

    crossfade 専用だった `slideshow.py` の `_rect_difference()` をここへ移した
    （遷移の描画ロジックを1か所に集約するため）。
    """
    inter = base.clip(cover)
    if inter.width <= 0 or inter.height <= 0:
        # まったく重なっていない。base 全体がはみ出し領域になる
        return [base] if base.width > 0 and base.height > 0 else []

    parts: list[pg.Rect] = []
    if inter.top > base.top:
        parts.append(pg.Rect(base.left, base.top, base.width, inter.top - base.top))
    if inter.bottom < base.bottom:
        parts.append(pg.Rect(base.left, inter.bottom, base.width, base.bottom - inter.bottom))
    if inter.left > base.left:
        parts.append(pg.Rect(base.left, inter.top, inter.left - base.left, inter.height))
    if inter.right < base.right:
        parts.append(pg.Rect(inter.right, inter.top, base.right - inter.right, inter.height))
    return parts


def _draw_crossfade(renderer: Any, dst_rect: 'DstRectFn', current: Any, next_tex: Any,
                     t: float) -> None:
    """
    現在の写真の上に次の写真を alpha でクロスフェードさせる（既定の遷移）。

    次の写真に覆われない帯（アスペクト比が変わったときの余白）は、
    覆われる領域と同じ進行度で黒へ落とす。これを怠るとフェード完了時に
    帯が瞬間的に消えて見える（.claude/architecture.md「対で更新が必要な箇所」参照）。
    余白込みの全画面クロスフェードと合成結果が厳密に一致する
    （重なり部分 C*(1-t)+N*t / はみ出し C*(1-t)）。
    """
    alpha = _alpha_from_ratio(t)
    if current is not None:
        current.alpha = 255
        current.draw(dstrect=dst_rect(current))
    if next_tex is not None:
        if current is not None:
            for rect in rect_difference(dst_rect(current), dst_rect(next_tex)):
                renderer.fill_rect(rect, (0, 0, 0), alpha)
        next_tex.alpha = alpha
        next_tex.draw(dstrect=dst_rect(next_tex))


def _draw_fade_black(renderer: Any, dst_rect: 'DstRectFn', current: Any, next_tex: Any,
                      t: float) -> None:
    """
    現在の写真を黒へフェードアウトし、真っ黒を経由してから次の写真をフェードインする。

    前半 (t<0.5) は current を不透明のまま描き、黒い全画面矩形を alpha=2t で
    重ねて暗くする。後半 (t>=0.5) は current を描かず、next を alpha=(2t-1) で
    フェードインする（背景は毎フレームの `clear()` で既に黒いため、追加の
    黒塗りは不要）。前半と後半で描く対象が切り替わるため、テクスチャは常に
    どちらか1枚しか描かない（常駐2枚の枠内に収まる）。
    """
    if t < 0.5:
        if current is not None:
            current.alpha = 255
            current.draw(dstrect=dst_rect(current))
        alpha = _alpha_from_ratio(2.0 * t)
        renderer.fill_rect(pg.Rect(0, 0, *renderer.size), (0, 0, 0), alpha)
    else:
        if next_tex is not None:
            alpha = _alpha_from_ratio(2.0 * t - 1.0)
            next_tex.alpha = alpha
            next_tex.draw(dstrect=dst_rect(next_tex))


def _draw_slide(renderer: Any, dst_rect: 'DstRectFn', current: Any, next_tex: Any,
                t: float) -> None:
    """
    右から左へ押し出すように切り替える。

    現在の写真を左へ、次の写真を右から押し込むように動かす。どちらも
    alpha=255（不透明）のままで、重なりの合成は発生しない。画面外へ出た
    部分は SDL が自動的にクリップするため、こちら側で矩形を縮める必要はない。
    イーズイン・イーズアウトをかけるのは、等速だと機械的でスライドショーの
    雰囲気に合わないため。
    """
    width, _height = renderer.size
    e = _ease(t)
    if current is not None:
        current.alpha = 255
        rect = dst_rect(current).move(-round(e * width), 0)
        current.draw(dstrect=rect)
    if next_tex is not None:
        next_tex.alpha = 255
        rect = dst_rect(next_tex).move(round((1.0 - e) * width), 0)
        next_tex.draw(dstrect=rect)


def _draw_wipe(renderer: Any, dst_rect: 'DstRectFn', current: Any, next_tex: Any,
              t: float) -> None:
    """
    右から左へ境界線が動き、通過した領域だけ次の写真に置き換わる。

    現在の写真をそのまま全体に描いたうえで、境界より右側を黒く塗り、
    その黒い領域と次の写真の描画先矩形が重なる部分だけを、対応する
    テクスチャ上の位置から**等倍で**切り出して描く（禁止パターン: 描画パスでの
    リサイズ）。次の写真の一部だけを覆う場合と全部を覆う場合の両方で
    `srcrect` と `dstrect` は常に同じ大きさになる。
    """
    width, height = renderer.size
    e = _ease(t)
    if current is not None:
        current.alpha = 255
        current.draw(dstrect=dst_rect(current))

    boundary_x = round(width * (1.0 - e))
    if boundary_x >= width:
        # e=0（フェード開始直後）はまだ何も置き換わっていない
        return

    black_rect = pg.Rect(boundary_x, 0, width - boundary_x, height)
    renderer.fill_rect(black_rect, (0, 0, 0), 255)

    if next_tex is None:
        return
    next_rect = dst_rect(next_tex)
    inter = next_rect.clip(black_rect)
    if inter.width <= 0 or inter.height <= 0:
        return
    src_rect = pg.Rect(inter.x - next_rect.x, inter.y - next_rect.y, inter.width, inter.height)
    next_tex.alpha = 255
    next_tex.draw(srcrect=src_rect, dstrect=inter)
