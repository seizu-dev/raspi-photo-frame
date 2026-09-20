import logging
import random
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.config_manager import ConfigManager
    from src.immich_api import ImmichAPI
    from src.photo_cache import PhotoCache

logger = logging.getLogger(__name__)


class PhotoSource:
    """
    Immich API とディスクキャッシュを繋ぐ層

    GUI からネットワークとキャッシュの使い分けを見えなくする。
    ネットワーク断はキャッシュへフォールバックする正常系として扱う。
    """

    def __init__(self, config: 'ConfigManager', api: 'ImmichAPI', cache: 'PhotoCache') -> None:
        self.config = config
        self.api = api
        self.cache = cache

    def cache_key(self) -> str:
        """ 取得元の組み合わせから決まるキャッシュキー（photo-frame 踏襲） """
        source = self.config.get('source')
        if source == 'daily_pickup':
            return f'daily_pickup_{date.today()}'
        if source == 'album':
            return self.config.get('album_id') or 'album_unset'
        return f'favorites_{self.config.get("display_mode")}'

    def load_list(self, force: bool = False) -> list[dict[str, Any]]:
        """
        写真リストを取得する。

        1. キャッシュが生きていればそれを使う（force=True なら飛ばす）
        2. API を叩き、取れたらキャッシュへ保存する
        3. API が空を返したら、失効したキャッシュでも使う（通信断での表示継続）

        display_mode == 'random' のシャッフルはここで行う。immich_api からは
        表示ポリシーとして意図的に外してある。
        """
        key = self.cache_key()

        # デイリーピックアップはキーに日付を含むため、過去日付の JSON が
        # 溜まり続ける。キャッシュヒット・ミスのどちらでも通るこの位置で掃除する
        # （取得できたときだけ掃除すると、日付が変わった直後に再起動して
        # キャッシュヒットした場合に古いファイルが残る）。
        if self.config.get('source') == 'daily_pickup':
            self.cache.cleanup_list_cache('daily_pickup_', key)

        if not force:
            cached = self.cache.get_asset_list(key)
            if cached:
                logger.info('写真リストをキャッシュから読み込みました: %s (%d 件)', key, len(cached))
                return self._ordered(cached)

        assets = self.api.fetch_assets_info()
        if assets:
            self.cache.store_asset_list(key, assets)
            return self._ordered(assets)

        stale = self.cache.get_asset_list(key, ignore_lifetime=True)
        if stale:
            logger.warning('API から取得できないため、失効したキャッシュで継続します: %s (%d 件)',
                           key, len(stale))
            return self._ordered(stale)

        logger.error('写真リストを取得できませんでした: %s', key)
        return []

    def _ordered(self, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """ 表示順を決める。元のリストは壊さない """
        result = list(assets)
        if self.config.get('display_mode') == 'random':
            random.shuffle(result)
        return result

    def ensure_photo(self, asset_id: str) -> Path | None:
        """
        表示解像度で確定済みの写真をディスクに用意し、そのパスを返す。

        **ネットワークアクセスを伴うため、必ずワーカースレッドから呼ぶこと。**
        メインスレッドから呼ぶと描画が止まる。
        """
        path = self.cache.get_photo_path(asset_id)
        if path is not None:
            return path

        raw = self.api.download_asset(asset_id, size='preview')
        if raw is None:
            return None
        return self.cache.store_photo(asset_id, raw)
