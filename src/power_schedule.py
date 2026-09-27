"""
時間帯による省電力モードの切り替え（デジタルサイネージ運用向け）

デジタルサイネージとしての利用時に、営業時間などの時間帯に応じて画面の
点灯・消灯の挙動を切り替えるための純粋関数を置く。`main.py` の
`_update_power_saving()` がこれらを使って実効モードを求め、副作用
（`DisplayManager` の操作やログ出力）はそちら側で行う。ヘッドレスで
境界値をテストしやすくするため、このモジュールには副作用を持ち込まない。
"""

import logging
from datetime import datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 実効モードの3種類。always_on/force_off は SPECIFICATION.md の用語に合わせた
MODE_ALWAYS_ON = 'always_on'
MODE_NORMAL = 'normal'
MODE_FORCE_OFF = 'force_off'

# power_schedule_off_hours が取りうる値
OFF_HOURS_VALUES = (MODE_NORMAL, MODE_FORCE_OFF)

# 既定値。power_schedule_enabled=false のときと合わせて「現行と同じ挙動」になる
DEFAULT_ENABLED = False
DEFAULT_START = 540  # 09:00
DEFAULT_END = 1080  # 18:00
DEFAULT_OFF_HOURS = MODE_NORMAL

MINUTES_PER_DAY = 1440

# power_schedule_start / power_schedule_end の有効範囲の上限。設定画面の Slider
# （`gui/screens/settings.py` の `_ROWS`）は `min_value=0` / `max_value=1410` /
# `step=30` で作ってあり、23:30 が最後の刻み（1440 分ちょうど＝24:00 は選べない）。
# ここを 1439（MINUTES_PER_DAY - 1）のままにすると、Slider では絶対に作れない
# 1411〜1439 の値（手で settings.json を編集した場合など）を「有効」として通して
# しまい、表示と設定ファイルの許容範囲がずれる。**この定数を変えるときは
# settings.py 側の max_value も必ず合わせること**（.claude/architecture.md
# 「対で更新が必要な箇所」参照）。30 の倍数でない値（545 等）はここでは拒否しない
# （Slider は 30分刻みに丸めるが、手編集や将来の刻み変更を締め出さないため）。
MAX_MINUTE = 1410

# 不正値の警告は (キー, repr(値)) の組につき1回だけ出す。`transition` 設定が
# 非文字列でクラッシュループした既知の問題（known-issues.md）と同じ轍を踏まないよう、
# unhashable な値（list/dict）でも repr() をキーにすることで set へ安全に入れられるようにする
_warned: set[tuple[str, str]] = set()


class _ConfigLike(Protocol):
    """ ConfigManager が持つ `.get(key, default)` だけを要求するプロトコル """

    def get(self, key: str, default: Any = None) -> Any: ...


def _warn_once(key: str, value: Any, message: str) -> None:
    """ 同じ (キー, repr(値)) の組については警告を1回だけ出す """
    marker = (key, repr(value))
    if marker in _warned:
        return
    _warned.add(marker)
    logger.warning(message)


def _normalize_enabled(config: _ConfigLike) -> bool:
    value = config.get('power_schedule_enabled', DEFAULT_ENABLED)
    if isinstance(value, bool):
        return value
    _warn_once('power_schedule_enabled', value,
               f'power_schedule_enabled の値が不正です（bool ではありません）: {value!r}。'
               f'既定値 {DEFAULT_ENABLED!r} を使います')
    return DEFAULT_ENABLED


def _normalize_minute(key: str, config: _ConfigLike, default: int) -> int:
    value = config.get(key, default)
    # bool は int のサブクラスなので isinstance(value, (int, float)) だけでは
    # True/False を通してしまう。先に弾く
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _warn_once(key, value,
                   f'{key} の値が不正です（数値ではありません）: {value!r}。'
                   f'既定値 {default} を使います')
        return default
    minute = int(round(value))
    if not (0 <= minute <= MAX_MINUTE):
        _warn_once(key, value,
                   f'{key} の値が範囲外です（0〜{MAX_MINUTE} 分）: {value!r}。'
                   f'既定値 {default} を使います')
        return default
    return minute


def _normalize_off_hours(config: _ConfigLike) -> str:
    value = config.get('power_schedule_off_hours', DEFAULT_OFF_HOURS)
    if isinstance(value, str) and value in OFF_HOURS_VALUES:
        return value
    _warn_once('power_schedule_off_hours', value,
               f'power_schedule_off_hours の値が不正です: {value!r}。'
               f'既定値 {DEFAULT_OFF_HOURS!r} を使います')
    return DEFAULT_OFF_HOURS


def normalize(config: _ConfigLike) -> tuple[bool, int, int, str]:
    """
    設定値を検証済みの (enabled, start, end, off_hours) へ正規化する。

    不正値（型違い・範囲外）は既定値へフォールバックする。警告ログは
    同じ (キー, repr(値)) の組につき1回だけ出す（`settings.json` を手で壊した
    場合に毎フレーム警告が出ないようにするため）。
    """
    enabled = _normalize_enabled(config)
    start = _normalize_minute('power_schedule_start', config, DEFAULT_START)
    end = _normalize_minute('power_schedule_end', config, DEFAULT_END)
    off_hours = _normalize_off_hours(config)
    return enabled, start, end, off_hours


def is_within(minute_of_day: int, start: int, end: int) -> bool:
    """
    `minute_of_day` が [start, end) の時間帯に含まれるかを返す。

    start < end は同日内の時間帯。start > end は日跨ぎ（例: 22:00〜06:00）を表し、
    minute >= start または minute < end のどちらかを満たせば時間帯内とみなす。
    start == end は「時間帯なし」を意味し、常に False を返す
    （00:00〜24:00 のような全日指定を this 関数では表現しない）。
    """
    if start == end:
        return False
    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def resolve_mode(config: _ConfigLike, now: datetime) -> str:
    """
    現在時刻から実効モード（always_on / normal / force_off）を求める。

    無効なら常に normal（現行どおりの自動消灯）。有効で時間帯内なら
    always_on。有効で時間帯外なら `power_schedule_off_hours` の値
    （normal または force_off）を返す。
    """
    enabled, start, end, off_hours = normalize(config)
    if not enabled:
        return MODE_NORMAL

    minute_of_day = now.hour * 60 + now.minute
    if is_within(minute_of_day, start, end):
        return MODE_ALWAYS_ON
    return off_hours
