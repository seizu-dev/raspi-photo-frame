import logging
import random
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config_manager import ConfigManager

logger = logging.getLogger(__name__)


class DailyPickupManager:
    """
    デイリーピックアップのローテーションロジックを管理するクラス。

    全アルバムを1周する間に重複が起きないよう、ランダムな順序でアルバムをキューに積み、
    毎日 N 件ずつ取り出す。キューが枯渇したら新しいサイクルを開始する。
    """

    def __init__(self, config_manager: 'ConfigManager') -> None:
        self.config = config_manager

    def get_today_album_ids(self, all_album_ids: list[str]) -> list[str]:
        """
        今日表示すべきアルバムIDのリストを返す。

        - 同じ日の呼び出しでは同じ選択を返す
        - 日付が変わったら次の N 件を取り出す
        - キューが足りなくなったら新サイクルを開始して補充する
        """
        if not all_album_ids:
            return []

        today = str(date.today())
        count = max(1, self.config.get('daily_pickup_count', 3))
        last_date = self.config.get('daily_pickup_date', '')
        selected_ids = self.config.get('daily_pickup_selected_ids', [])
        remaining_ids = self.config.get('daily_pickup_remaining_ids', [])

        # 同じ日はキャッシュされた選択を返す（存在するアルバムのみ）
        if last_date == today and selected_ids:
            all_set = set(all_album_ids)
            return [aid for aid in selected_ids if aid in all_set]

        # 日付が変わった → ローテーションを進める
        all_set = set(all_album_ids)

        # 残キューを現存するアルバムのみに絞る
        remaining_ids = [aid for aid in remaining_ids if aid in all_set]

        selected: list[str] = []

        if len(remaining_ids) >= count:
            # 残キューから必要数を取り出す
            selected = remaining_ids[:count]
            remaining_ids = remaining_ids[count:]
        else:
            # 残キューが不足 → 全部使い切って新サイクルを開始
            selected = list(remaining_ids)

            selected_set = set(selected)
            new_cycle = [aid for aid in all_album_ids if aid not in selected_set]
            random.shuffle(new_cycle)

            needed = count - len(selected)
            selected += new_cycle[:needed]
            remaining_ids = new_cycle[needed:]

        # 状態を保存
        self.config.set('daily_pickup_date', today)
        self.config.set('daily_pickup_selected_ids', selected)
        self.config.set('daily_pickup_remaining_ids', remaining_ids)

        logger.info('デイリーピックアップ: %d 件を選択（残キュー %d 件）', len(selected), len(remaining_ids))
        return selected
