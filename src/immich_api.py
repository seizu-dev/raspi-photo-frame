import logging
import os
from typing import TYPE_CHECKING, Any, Callable

import requests

from src.daily_pickup_manager import DailyPickupManager
from src.photo_provider import ProviderError

if TYPE_CHECKING:
    from src.config_manager import ConfigManager

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
# ページ送りが終わらない場合の安全弁。Immich の検索 API は nextPage を返すが、
# 実レスポンスは未確認のため上限を設けて無限ループを防ぐ。
MAX_SEARCH_PAGES = 100


class ImmichAPI:
    """
    Immich API との通信を管理するクラス

    HTTP クライアント層に責務を限定する。写真リストのキャッシュ判定と画像のリサイズは
    photo_cache.py の担当であり、このクラスは常に API を叩いて結果をそのまま返す。
    """

    def __init__(self, settings_manager: 'ConfigManager',
                 status_callback: Callable[[str], None] | None = None,
                 base_url: str | None = None, api_key: str | None = None) -> None:
        # 秘匿情報はイメージに焼かず .env から環境変数として渡す（本番・Dev Container 共通）
        self.api_url = (base_url or os.environ.get('IMMICH_BASE_URL') or '').rstrip('/')
        self.api_key = api_key or os.environ.get('IMMICH_API_KEY') or ''
        if not self.api_url or not self.api_key:
            raise ValueError('IMMICH_BASE_URL と IMMICH_API_KEY を .env に設定してください。')

        self.headers = {'x-api-key': self.api_key, 'Accept': 'application/json'}
        self.settings = settings_manager
        self._status_callback = status_callback

        # PhotoProvider Protocol を満たすための属性（src/photo_provider.py）。
        # Immich は既存キャッシュを温存するため cache_namespace は空文字にする
        # （PhotoCache は空文字を「名前空間なし＝従来どおりの直下配置」として扱う）。
        self.name = 'immich'
        self.cache_namespace = ''
        self.supports_favorites = True
        self.delivers_originals = False
        # Immich は表紙を差し替えると albumThumbnailAssetId 自体が変わるため、
        # 期限切れによる作り直しは不要（photo_provider.py の Protocol docstring参照）。
        self.album_thumbnail_expires = False

    def update_status(self, message: str) -> None:
        """ 進捗を通知する。コールバック未指定でも実機の調査手段としてログには必ず残す """
        logger.info(message)
        if self._status_callback:
            self._status_callback(message)

    def fetch_assets_info(self) -> list[dict[str, Any]]:
        """
        設定された取得元に応じて、表示する写真のアセット情報リストを取得する。

        キャッシュ判定は行わず常に API を叩く。ネットワーク断はキャッシュへ
        フォールバックする正常系として扱うため、通信エラー時は空リストを返す。
        表示順のシャッフル（display_mode == 'random'）は表示層の責務なので行わない。
        """
        source = self.settings.get('source')
        try:
            if source == 'daily_pickup':
                assets_info = self._fetch_daily_pickup_assets_info()
            elif source == 'album':
                assets_info = self._fetch_album_assets_info(self.settings.get('album_id'))
            else:
                assets_info = self._fetch_favorite_assets_info()
        except requests.exceptions.RequestException as e:
            self.update_status(f'API接続エラー: {e}')
            return []

        if not assets_info:
            self.update_status('写真が見つかりませんでした。設定を確認してください。')
            return []

        self.update_status(f'写真情報を {len(assets_info)} 件取得しました。')
        return assets_info

    def _to_asset_info(self, item: dict[str, Any], album_name: str = '') -> dict[str, Any]:
        """ API のアセットを表示に必要な最小限の辞書へ落とす（リスト全体を抱えるため軽く保つ） """
        exif = item.get('exifInfo') or {}
        info = {
            'id': item['id'],
            'description': item.get('description') or exif.get('description') or '',
            'date': exif.get('dateTimeOriginal') or item.get('fileCreatedAt'),
        }
        if album_name:
            info['album_name'] = album_name
        return info

    def _fetch_album_assets_info(self, album_id: str | None) -> list[dict[str, Any]]:
        """ 指定アルバムのアセット情報を取得する。アルバムのソート順を反映する """
        return self._fetch_album_assets_raw(album_id, apply_album_order=True)

    def _fetch_album_assets_raw(self, album_id: str | None,
                                apply_album_order: bool) -> list[dict[str, Any]]:
        """
        指定アルバムのアセット情報を取得する内部実装。

        `apply_album_order` はアルバムの `order`（'asc'/'desc'）を反映するかどうか。
        単一アルバム選択（`_fetch_album_assets_info`）では反映するが、デイリーピックアップは
        旧実装（移植元）がアルバムの `order` を一切見ていなかったため、その挙動を
        崩さないよう `PhotoSource` から False で呼ばれる（`fetch_album_assets()` を参照）。
        """
        if not album_id:
            return []

        album_response = self._get(f'/api/albums/{album_id}')
        items = album_response.get('assets', [])
        if not isinstance(items, list):
            return []

        assets_info = [self._to_asset_info(item) for item in items]
        # API は常に降順(desc)で返される前提。'asc' のときのみ反転する
        if apply_album_order and album_response.get('order', 'desc') == 'asc':
            assets_info.reverse()
        return assets_info

    def _fetch_favorite_assets_info(self) -> list[dict[str, Any]]:
        """ お気に入りのアセット情報を取得する。検索 API はページ分割されるため follow する """
        items: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_SEARCH_PAGES:
            response = self._post('/api/search/metadata', {'isFavorite': True, 'page': page})
            # POST /api/search のレスポンスは {"assets": {"total": N, "items": [...]}} という構造
            assets = response.get('assets') or {}
            items.extend(assets.get('items') or [])

            next_page = assets.get('nextPage')
            if not next_page:
                break
            try:
                page = int(next_page)
            except (TypeError, ValueError):
                logger.warning('nextPage を解釈できないためページ送りを打ち切ります: %r', next_page)
                break
        else:
            logger.warning('検索のページ送りが上限 %d に達しました。取得漏れの可能性があります。',
                           MAX_SEARCH_PAGES)

        return [self._to_asset_info(item) for item in items]

    def _fetch_daily_pickup_assets_info(self) -> list[dict[str, Any]]:
        """ デイリーピックアップ用のアセット情報を取得する """
        all_albums = self.fetch_albums()
        if not all_albums:
            return []

        all_album_ids = [a['id'] for a in all_albums]
        album_name_map = {a['id']: a['albumName'] for a in all_albums}

        pickup_mgr = DailyPickupManager(self.settings)
        selected_album_ids = pickup_mgr.get_today_album_ids(all_album_ids)
        logger.info('デイリーピックアップ対象: %s',
                    [album_name_map.get(aid, aid) for aid in selected_album_ids])

        assets_info: list[dict[str, Any]] = []
        for album_id in selected_album_ids:
            try:
                album_response = self._get(f'/api/albums/{album_id}')
            except requests.exceptions.RequestException as e:
                # 1つのアルバムが取れなくても残りは表示したいので、ここだけは継続する
                self.update_status(f'アルバム取得エラー (ID: {album_id}): {e}')
                continue

            album_name = album_name_map.get(album_id, '')
            for item in album_response.get('assets', []):
                assets_info.append(self._to_asset_info(item, album_name))

        return assets_info

    def fetch_albums(self) -> list[dict[str, Any]]:
        """ Immich からアルバムのリストを取得する（自分のアルバム＋共有アルバム） """
        try:
            own_albums = self._get('/api/albums')
            shared_albums = self._get('/api/albums', params={'shared': 'true'})
        except requests.exceptions.RequestException as e:
            self.update_status(f'アルバム取得エラー: {e}')
            return []

        # ID で重複排除してマージ
        seen_ids = {a['id'] for a in own_albums}
        return own_albums + [a for a in shared_albums if a['id'] not in seen_ids]

    # ------------------------------------------------------ PhotoProvider Protocol

    def fetch_album_assets(self, album_id: str, album_name: str = '',
                           apply_album_order: bool = True) -> list[dict[str, Any]]:
        """
        PhotoProvider Protocol 用の窓口。`_fetch_album_assets_raw` を呼び、
        `requests` の例外は `ProviderError` に変換する（呼び出し側の
        `photo_source.py` が取得元の実装に依存せずに済むようにするため）。

        `album_name` を渡すと各アセットへ付与する。デイリーピックアップのように
        複数アルバムを混ぜて表示する場合、説明文の `[アルバム名]` に使われる。
        単一アルバム選択（source == 'album'）では渡さない（従来どおり
        `config.get('album_name')` にフォールバックする）。

        `apply_album_order` は既定で True（単一アルバム選択の従来どおりの挙動）。
        デイリーピックアップ（`photo_source.py` の `_fetch_daily_pickup_assets_info`）は
        旧実装がアルバムの `order` を見ていなかったため、False を渡して反転しない。
        """
        try:
            items = self._fetch_album_assets_raw(album_id, apply_album_order=apply_album_order)
        except requests.exceptions.RequestException as e:
            raise ProviderError(str(e)) from e
        if album_name:
            for item in items:
                item['album_name'] = album_name
        return items

    def fetch_favorite_assets(self) -> list[dict[str, Any]]:
        """ PhotoProvider Protocol 用の窓口。既存の `_fetch_favorite_assets_info` を呼ぶ """
        try:
            return self._fetch_favorite_assets_info()
        except requests.exceptions.RequestException as e:
            raise ProviderError(str(e)) from e

    def fetch_photo(self, asset_id: str) -> bytes | None:
        """ PhotoProvider Protocol 用の窓口。表示用の preview サイズをそのまま返す """
        return self.download_asset(asset_id, size='preview')

    def fetch_album_thumbnail(self, album: dict[str, Any]) -> bytes | None:
        """ PhotoProvider Protocol 用の窓口。アルバムのサムネイル用アセットを取得する """
        thumb_id = album.get('albumThumbnailAssetId')
        if not thumb_id:
            return None
        return self.download_asset(thumb_id, size='thumbnail')

    def download_asset(self, asset_id: str, size: str = 'preview',
                       timeout: int = DEFAULT_TIMEOUT) -> bytes | None:
        """
        アセットの画像データをそのままバイト列で返す。

        リサイズもキャッシュも行わない。表示解像度への変換は photo_cache.py が
        キャッシュ生成時に一度だけ行う契約になっている。
        """
        try:
            response = requests.get(self.get_thumbnail_url(asset_id, size),
                                    headers=self.headers, timeout=timeout)
            response.raise_for_status()
            return response.content
        except requests.exceptions.RequestException as e:
            self.update_status(f'画像取得エラー (ID: {asset_id}, size: {size}): {e}')
        return None

    def get_thumbnail_url(self, asset_id: str, size: str = 'preview') -> str:
        return f'{self.api_url}/api/assets/{asset_id}/thumbnail?size={size}'

    def get_asset_download_url(self, asset_id: str) -> str:
        return f'{self.api_url}/api/assets/{asset_id}/download'

    def _get(self, endpoint: str, **kwargs: Any) -> Any:
        kwargs.setdefault('timeout', DEFAULT_TIMEOUT)
        response = requests.get(self.api_url + endpoint, headers=self.headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def _post(self, endpoint: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        kwargs.setdefault('timeout', DEFAULT_TIMEOUT)
        response = requests.post(self.api_url + endpoint, headers=self.headers,
                                 json=payload, **kwargs)
        response.raise_for_status()
        return response.json()
