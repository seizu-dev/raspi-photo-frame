"""
UI の多言語対応（日本語 / 英語）と日時の書式選択

`t(key)` で現在言語の文言を引く。文言は Python の辞書に埋め込み、外部 JSON には
しない（`config/settings.json` と違って翻訳文はユーザーが編集する対象ではないため、
1ファイルにまとめた方が「キーを足したのに片方の言語だけ忘れる」事故を見つけやすい）。

**状態はすべてモジュールグローバルで持つ。** `ConfigManager` を全ウィジェットへ
配って歩く設計にしないための割り切りで、`main.py` が起動時と設定変更時に
`set_language()` を呼ぶだけで全画面へ伝わる。既存の SDL `generation`
（`.claude/architecture.md`「対で更新が必要な箇所」）と同じ相乗りの形にするため、
`generation` を関数で公開している。`from src import i18n` した側が `i18n.generation()`
を毎回呼び直せば最新値が読めるが、`from src.i18n import generation` のように
値を直接 import されると束縛がその時点の整数に固定され、以後の更新が伝わらない
（モジュール属性の再代入は import 元の名前空間には反映されない）。
"""

import logging
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

LANGUAGES: tuple[str, ...] = ('ja', 'en')
DEFAULT_LANGUAGE = 'ja'

TIME_FORMATS: tuple[str, ...] = ('24h', '12h')
DEFAULT_TIME_FORMAT = '24h'

DATE_FORMATS: tuple[str, ...] = ('ymd_slash', 'mdy_slash', 'dmy_slash', 'long')
DEFAULT_DATE_FORMAT = 'ymd_slash'

# --------------------------------------------------------------------- 状態

_current_language = DEFAULT_LANGUAGE
_generation = 0

# t() の警告を「同じ理由につき1回」に絞るための記録。t() は写真の説明文や
# オーバーレイなど毎フレーム呼ばれうる経路から使われるため、警告そのものを
# 出し続けると `known-issues.md` の「毎フレーム出力するログを入れない」に反する。
# キーは (種別, 詳細) のタプルで持つ。詳細側は常に str / int 等ハッシュ可能な値に
# 変換してから入れること（`normalize_fit()` と同じ理由。src/photo_cache.py 参照）。
_warned: set[tuple[str, str]] = set()


def _warn_once(category: str, detail: Any, message: str, *args: Any) -> None:
    """ (category, detail) の組み合わせにつき1回だけ logger.warning する """
    try:
        detail_key = detail if isinstance(detail, str) else repr(detail)
    except Exception:
        # repr() 自体が例外を投げる自作オブジェクトも理論上ありうるため、
        # 警告を出すこと自体でクラッシュしないよう最後の砦を用意する
        detail_key = '<unrepr>'
    key = (category, detail_key)
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


# ------------------------------------------------------------------- 正規化

def normalize_language(value: Any) -> str:
    """ 不正値は DEFAULT_LANGUAGE へフォールバックする（警告は種別ごとに1回） """
    if isinstance(value, str) and value in LANGUAGES:
        return value
    _warn_once('invalid_language', value,
               '未知の言語設定です。%s として扱います: %r', DEFAULT_LANGUAGE, value)
    return DEFAULT_LANGUAGE


def normalize_time_format(value: Any) -> str:
    if isinstance(value, str) and value in TIME_FORMATS:
        return value
    _warn_once('invalid_time_format', value,
               '未知の time_format 設定値です。%s として扱います: %r',
               DEFAULT_TIME_FORMAT, value)
    return DEFAULT_TIME_FORMAT


def normalize_date_format(value: Any) -> str:
    if isinstance(value, str) and value in DATE_FORMATS:
        return value
    _warn_once('invalid_date_format', value,
               '未知の date_format 設定値です。%s として扱います: %r',
               DEFAULT_DATE_FORMAT, value)
    return DEFAULT_DATE_FORMAT


# --------------------------------------------------------------- 言語の状態

def set_language(lang: Any) -> None:
    """
    現在言語を切り替える。

    **値が実際に変わったときだけ `generation` を +1 する。** 3画面
    （menu / settings / album）は `renderer.generation` と同じ形で「前回控えた
    世代と違えば作り直す」実装になる想定のため、同じ言語を再設定しただけで
    世代を進めると、呼ぶたびに全画面が無駄に再構築されてしまう。
    """
    global _current_language, _generation
    normalized = normalize_language(lang)
    if normalized != _current_language:
        _current_language = normalized
        _generation += 1


def current_language() -> str:
    return _current_language


def generation() -> int:
    """
    言語世代を返す。**関数で公開する**のはモジュール先頭の docstring に書いた
    束縛固定の問題を避けるため。呼び出し側は `i18n.generation()` の形で毎回呼ぶこと。
    """
    return _generation


# --------------------------------------------------------------------- t()

def t(key: Any, **kwargs: Any) -> str:
    """
    現在言語の文言を返す。

    - `key` が `str` でない場合はフォーマットできないため、警告して
      `str(key)` を返す（`known-issues.md`「`transition` に文字列以外が入ると
      クラッシュループしていた」と同じ事故を避けるため、`_warned` へ入れる前に
      必ず `isinstance` で確かめる）
    - 辞書に無いキーは en へ、en も無ければキー文字列そのものへフォールバックする
    - `kwargs` があれば `str.format(**kwargs)` する。プレースホルダが合わず
      `KeyError` / `IndexError` になったら、警告してフォーマット前の文字列を返す
      （設定ミスや訳文の書き間違いでアプリを落とさないため）
    """
    if not isinstance(key, str):
        _warn_once('non_str_key', type(key).__name__,
                   't() に文字列以外のキーが渡されました: %r', key)
        return str(key)

    entry = _TEXTS.get(key)
    if entry is None:
        _warn_once('missing_key', key, '未知の翻訳キーです: %s', key)
        text = key
    else:
        text = entry.get(_current_language)
        if text is None:
            text = entry.get('en')
        if text is None:
            text = key

    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError):
        _warn_once('format_error', key,
                   '翻訳文のフォーマットに失敗しました: key=%s kwargs=%r', key, kwargs)
        return text


# ------------------------------------------------------------- 日時フォーマット

def format_time(when: Any = None, fmt: str = DEFAULT_TIME_FORMAT) -> str:
    """
    時刻を書式に従って整形する。

    `when` は `time.struct_time` / epoch（`int` / `float`） / `None`（現在時刻）
    のいずれかを受け付ける。**`strftime('%p')` は使わない**（コンテナの locale は
    C 固定で、`%p` は常に `AM`/`PM` になり ja の `午前`/`午後` が出せないため）。
    午前午後・時刻の並び順は辞書と自前の組み立てで作る。
    """
    fmt = normalize_time_format(fmt)

    if when is None:
        struct = time.localtime()
    elif isinstance(when, (int, float)):
        struct = time.localtime(when)
    else:
        struct = when

    hour = struct.tm_hour
    minute = struct.tm_min

    if fmt == '24h':
        return f'{hour:02d}:{minute:02d}'

    # 12時制。午前午後の境界は共通だが、0時・12時の表示は言語で規則が違う。
    period_key = 'datetime.am' if hour < 12 else 'datetime.pm'
    period = t(period_key)
    hour12 = hour % 12

    if current_language() == 'ja':
        # 日本語の12時制は 0〜11 時をそのまま使う（時計アプリやテレビの表記の慣習。
        # 0:05 は「午前0:05」、12:30 は「午後0:30」。英語のように 0 時・12 時を
        # 「12」に丸めない）。「午後2:30」のように前置・詰め書き
        return f'{period}{hour12}:{minute:02d}'

    # 英語の12時制は 0 時・12 時をどちらも「12」と表示する
    # （0:05 は 12:05 AM、12:30 は 12:30 PM）。「2:30 PM」のように後置・スペース区切り
    if hour12 == 0:
        hour12 = 12
    return f'{hour12}:{minute:02d} {period}'


def format_date(iso_text: str | None, fmt: str = DEFAULT_DATE_FORMAT) -> str:
    """
    Immich の ISO8601 文字列（例 `2024-03-12T15:55:18.092Z`）を書式に従って整形する。

    **タイムゾーン変換はしない。** 文字列に含まれる日付部分をそのまま使う。
    ここで UTC→ローカル変換を入れると、既存の表示（`raw[:10]`）から日付がずれる
    場合があり、要望の範囲外。`ymd_slash`（既定値）は現行の
    `raw[:10].replace('-', '/')` と一字一句同じ結果になることが前提
    （既存の `settings.json` に自動補完で `date_format` が増えたときに
    見た目が変わらないようにするため）。

    パースに失敗したら例外を投げず、現行どおり先頭10文字を `/` 区切りにして返す。
    空文字・`None` は空文字を返す。
    """
    fmt = normalize_date_format(fmt)

    if not iso_text:
        return ''

    dt: datetime | None = None
    try:
        # datetime.fromisoformat は Python 3.11 以降 'Z' サフィックスを解釈できる
        # （本プロジェクトは 3.13 のため実際には不要）。ここでの置換は
        # 3.10 以前（'Z' を解釈できない）でも動くようにするための実害の無い保険。
        dt = datetime.fromisoformat(iso_text.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        dt = None

    if dt is None:
        try:
            return iso_text[:10].replace('-', '/')
        except (TypeError, AttributeError):
            return ''

    if fmt == 'ymd_slash':
        return f'{dt.year:04d}/{dt.month:02d}/{dt.day:02d}'
    if fmt == 'mdy_slash':
        return f'{dt.month:02d}/{dt.day:02d}/{dt.year:04d}'
    if fmt == 'dmy_slash':
        return f'{dt.day:02d}/{dt.month:02d}/{dt.year:04d}'

    # long: 数値の桁揃えをしない（2024年3月12日 / Mar 12, 2024）
    month_text = t(f'datetime.month.{dt.month}')
    if current_language() == 'ja':
        return f'{dt.year}年{month_text}月{dt.day}日'
    return f'{month_text} {dt.day}, {dt.year}'


# ----------------------------------------------------------------------- 辞書
#
# キー単位で ja / en を並べる。言語単位（{'ja': {...}, 'en': {...}}）で分けると、
# キーを1つ足したときに片方の言語だけ書き忘れてもエディタ上で気づきにくいため。

_TEXTS: dict[str, dict[str, str]] = {
    # --- 共通 / メニュー -------------------------------------------------
    'common.back': {'ja': '戻る', 'en': 'Back'},
    'menu.title': {'ja': 'メニュー', 'en': 'Menu'},
    'menu.settings': {'ja': '基本設定', 'en': 'Settings'},
    'menu.album': {'ja': 'アルバム選択', 'en': 'Album'},
    'menu.quit': {'ja': 'アプリを終了', 'en': 'Quit App'},

    # --- 基本設定画面 ------------------------------------------------------
    'settings.title': {'ja': '基本設定', 'en': 'Settings'},

    'settings.group.locale': {'ja': '言語と表示形式', 'en': 'Language & Formats'},
    'settings.group.slideshow': {'ja': 'スライドショー', 'en': 'Slideshow'},
    'settings.group.display': {'ja': '画面表示', 'en': 'Display'},
    'settings.group.power': {'ja': '省電力', 'en': 'Power Saving'},
    'settings.group.photos': {'ja': '写真の取得', 'en': 'Photos'},

    'settings.row.language': {'ja': '表示言語', 'en': 'Language'},
    'settings.row.time_format': {'ja': '時刻の書式', 'en': 'Time format'},
    'settings.row.date_format': {'ja': '日付の書式', 'en': 'Date format'},

    'settings.row.interval': {'ja': 'スライドショー間隔 (秒)', 'en': 'Slide interval (sec)'},
    'settings.row.transition': {'ja': 'トランジションの種類', 'en': 'Transition type'},
    'settings.row.transition_duration': {
        'ja': 'トランジション時間 (秒)', 'en': 'Transition duration (sec)',
    },
    'settings.row.display_mode': {'ja': '写真の表示順', 'en': 'Photo order'},
    'settings.row.photo_fit': {'ja': '写真の表示方法', 'en': 'Photo fit'},
    'settings.row.show_clock': {'ja': '時計の表示', 'en': 'Show clock'},
    'settings.row.show_comment': {'ja': 'コメントの表示', 'en': 'Show comment'},
    'settings.row.comment_font_size': {
        'ja': 'コメントの文字サイズ', 'en': 'Comment font size',
    },
    'settings.row.show_countdown': {'ja': 'カウントダウンの表示', 'en': 'Show countdown'},
    'settings.row.show_memory_usage': {
        'ja': 'メモリ使用量を表示', 'en': 'Show memory usage',
    },
    'settings.row.power_saving_enabled': {
        'ja': '省電力を有効にする', 'en': 'Enable power saving',
    },
    'settings.row.power_saving_timeout': {
        'ja': 'スリープまでの時間 (秒)', 'en': 'Time to sleep (sec)',
    },
    'settings.row.display_wakeup_delay': {
        'ja': '復帰後の待ち時間 (秒)', 'en': 'Wake-up delay (sec)',
    },
    'settings.row.motion_sensor_enabled': {
        'ja': '人感センサーを有効にする', 'en': 'Enable motion sensor',
    },
    'settings.row.daily_pickup_count': {
        'ja': 'デイリーピックアップ数', 'en': 'Daily pickup count',
    },
    'settings.row.cache_lifetime_hours': {
        'ja': '写真リストの有効期間 (時間)', 'en': 'Photo list lifetime (hr)',
    },
    'settings.row.photo_cache_max_mb': {
        'ja': '画像キャッシュ上限 (MB)（0で無制限）',
        'en': 'Photo cache limit (MB) (0 = unlimited)',
    },

    # --- アルバム選択画面 ----------------------------------------------------
    'album.title': {'ja': 'アルバム選択', 'en': 'Select Album'},
    'album.confirm': {'ja': '決定', 'en': 'OK'},
    'album.loading': {'ja': 'アルバムを読み込み中...', 'en': 'Loading albums...'},
    'album.favorites': {'ja': 'お気に入り', 'en': 'Favorites'},
    'album.daily_pickup': {'ja': 'デイリーピックアップ', 'en': 'Daily Pickup'},

    # --- ステータス（main.py の set_status()） -----------------------------
    'status.no_immich': {'ja': 'Immich の設定がありません', 'en': 'Immich is not configured'},
    'status.loading_photos': {
        'ja': '写真情報を取得しています...', 'en': 'Loading photo information...',
    },
    'status.slideshow_start': {
        'ja': '{count}枚の写真でスライドショーを開始します。',
        'en': 'Starting slideshow with {count} photos.',
    },
    'status.no_photos': {
        'ja': '表示できる写真がありません。{seconds}秒後に再試行します。',
        'en': 'No photos to show. Retrying in {seconds} seconds.',
    },
    'status.album_needs_immich': {
        'ja': 'アルバム選択にはImmichの設定が必要です',
        'en': 'Album selection requires Immich to be configured',
    },

    # --- Spinner の選択肢（内部値は変えず、表示名だけ翻訳する） ------------------
    'value.transition.crossfade': {'ja': 'クロスフェード', 'en': 'Crossfade'},
    'value.transition.fade_black': {'ja': '黒フェード', 'en': 'Fade to black'},
    'value.transition.slide': {'ja': 'スライド', 'en': 'Slide'},
    'value.transition.wipe': {'ja': 'ワイプ', 'en': 'Wipe'},
    'value.transition.random': {'ja': 'ランダム', 'en': 'Random'},

    'value.display_mode.sequential': {'ja': '順番', 'en': 'Sequential'},
    'value.display_mode.random': {'ja': 'ランダム', 'en': 'Random'},

    'value.photo_fit.contain': {'ja': '内接', 'en': 'Contain'},
    'value.photo_fit.cover': {'ja': '外接', 'en': 'Cover'},
    'value.photo_fit.smart': {'ja': 'スマート', 'en': 'Smart'},

    # 言語名は「今の言語で読めなくなって戻せなくなる」のを防ぐため、
    # ja / en のどちらで表示していても同じ文字列にする
    'value.language.ja': {'ja': '日本語', 'en': '日本語'},
    'value.language.en': {'ja': 'English', 'en': 'English'},

    'value.time_format.24h': {'ja': '24時間', 'en': '24-hour'},
    'value.time_format.12h': {'ja': '12時間', 'en': '12-hour'},

    # 書式の実例が分かるよう、サンプル文字列そのものを表示名にする
    'value.date_format.ymd_slash': {'ja': '2024/03/12', 'en': '2024/03/12'},
    'value.date_format.mdy_slash': {'ja': '03/12/2024', 'en': '03/12/2024'},
    'value.date_format.dmy_slash': {'ja': '12/03/2024', 'en': '12/03/2024'},
    'value.date_format.long': {'ja': '2024年3月12日', 'en': 'Mar 12, 2024'},

    # --- 日時の組み立てに使う内部キー（UI 文言とは接頭辞で分ける） -----------------
    # ja は long 書式で桁揃えしない数値としてそのまま使う（「3月」など）
    'datetime.month.1': {'ja': '1', 'en': 'Jan'},
    'datetime.month.2': {'ja': '2', 'en': 'Feb'},
    'datetime.month.3': {'ja': '3', 'en': 'Mar'},
    'datetime.month.4': {'ja': '4', 'en': 'Apr'},
    'datetime.month.5': {'ja': '5', 'en': 'May'},
    'datetime.month.6': {'ja': '6', 'en': 'Jun'},
    'datetime.month.7': {'ja': '7', 'en': 'Jul'},
    'datetime.month.8': {'ja': '8', 'en': 'Aug'},
    'datetime.month.9': {'ja': '9', 'en': 'Sep'},
    'datetime.month.10': {'ja': '10', 'en': 'Oct'},
    'datetime.month.11': {'ja': '11', 'en': 'Nov'},
    'datetime.month.12': {'ja': '12', 'en': 'Dec'},
    'datetime.am': {'ja': '午前', 'en': 'AM'},
    'datetime.pm': {'ja': '午後', 'en': 'PM'},
}
