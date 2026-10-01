import logging
import random
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.daily_pickup_manager import DailyPickupManager
from src.photo_provider import ProviderError

if TYPE_CHECKING:
    from src.config_manager import ConfigManager
    from src.photo_cache import PhotoCache
    from src.photo_provider import PhotoProvider

logger = logging.getLogger(__name__)


class PhotoSource:
    """
    写真取得元（provider）とディスクキャッシュを繋ぐ層

    GUI から取得元の種類とネットワーク／キャッシュの使い分けを見えなくする。
    ネットワーク断はキャッシュへフォールバックする正常系として扱う。

    favorites/album/daily_pickup の振り分けは元々 `ImmichAPI.fetch_assets_info()`
    にあったが、取得元を抽象化する際にここへ移した
    （.claude/plans/abundant-weaving-kernighan.md PR1）。`provider` は
    `PhotoProvider` Protocol を満たす任意の取得元（PR1 時点では `ImmichAPI` のみ）。
    """

    def __init__(self, config: 'ConfigManager', provider: 'PhotoProvider',
                 cache: 'PhotoCache') -> None:
        self.config = config
        self.provider = provider
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

        1. キャッシュが生きていればそれを使う（force=True、または provider の
           `rescan_on_load` が True なら飛ばす）
        2. provider を叩き、取れたらキャッシュへ保存する
        3. provider が空を返したら、失効したキャッシュでも使う（通信断での表示継続）

        display_mode == 'random' のシャッフルはここで行う。provider からは
        表示ポリシーとして意図的に外してある。
        """
        key = self.cache_key()

        # デイリーピックアップはキーに日付を含むため、過去日付の JSON が
        # 溜まり続ける。キャッシュヒット・ミスのどちらでも通るこの位置で掃除する
        # （取得できたときだけ掃除すると、日付が変わった直後に再起動して
        # キャッシュヒットした場合に古いファイルが残る）。
        if self.config.get('source') == 'daily_pickup':
            self.cache.cleanup_list_cache('daily_pickup_', key)

        rescan = getattr(self.provider, 'rescan_on_load', False)
        # ローカルフォルダは中身がいつでも変わり、走査も安価で通信を伴わないため、
        # キャッシュが生きていても毎回取り直す。失敗（ProviderError）なら下の失効
        # キャッシュへ落ちる（走査成功で空の場合は下で空を返す）（Immich / Drive は False で従来どおり）。
        if not force and not rescan:
            cached = self.cache.get_asset_list(key)
            if cached:
                logger.info('写真リストをキャッシュから読み込みました: %s (%d 件)', key, len(cached))
                return self._ordered(cached)

        assets = self._fetch_assets_info()
        if assets:
            self.cache.store_asset_list(key, assets)
            return self._ordered(assets)

        if rescan and assets is not None:
            # 走査に成功して結果が空なのは「写真が無くなった」という正しい結果。
            # 失効キャッシュへ落とすと、消した写真が表示され続ける。キャッシュも
            # 空へ更新し、あとで I/O エラーになったときに消した写真が復活しない
            # ようにする（rescan_on_load の取得元だけ。Immich / Drive は通信断と
            # 区別できないので従来どおり下の失効キャッシュへ落ちる）。
            self.cache.store_asset_list(key, [])
            return []

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

    # ---------------------------------------------------------- アセット情報の取得

    def _update_status(self, message: str) -> None:
        """
        provider の `update_status()`（ログ＋オーバーレイ通知）があれば委譲する。

        `PhotoProvider` Protocol はこのメソッドを必須にしていない（すべての
        取得元がステータス通知を持つとは限らないため）ので、無い場合はログにだけ残す。
        """
        update_status = getattr(self.provider, 'update_status', None)
        if callable(update_status):
            update_status(message)
        else:
            logger.info(message)

    def _fetch_assets_info(self) -> list[dict[str, Any]] | None:
        """
        設定された取得元に応じて、表示する写真のアセット情報リストを取得する。

        キャッシュ判定は行わず常に provider を叩く。通信エラーはキャッシュへ
        フォールバックする正常系として扱うため、`ProviderError` は空リストに変換する。
        表示順のシャッフル（display_mode == 'random'）は `_ordered()` の責務なので
        ここでは行わない（元 `ImmichAPI.fetch_assets_info()` から移植。文言は変えていない）。

        戻り値の `None` は「`ProviderError`（取得に失敗した）」、空リストは「取得はできたが
        写真が無かった」を表す。呼び出し側（`load_list()`）で両者を区別するのは
        `rescan_on_load` の取得元だけで、それ以外は `None` も空も同じ扱い（従来どおり）。
        """
        source = self.config.get('source')
        try:
            if source == 'daily_pickup':
                assets_info = self._fetch_daily_pickup_assets_info()
            elif source == 'album':
                assets_info = self._fetch_album_assets_info(self.config.get('album_id'))
            elif self.provider.supports_favorites:
                assets_info = self.provider.fetch_favorite_assets()
            else:
                # reconcile_settings() が起動時に daily_pickup へ寄せるため通常は
                # 通らないが、settings.json を手で書き換えた場合の保険として残す
                assets_info = []
        except ProviderError as e:
            self._update_status(f'API接続エラー: {e}')
            return None

        if not assets_info:
            self._update_status('写真が見つかりませんでした。設定を確認してください。')
            return []

        self._update_status(f'写真情報を {len(assets_info)} 件取得しました。')
        return assets_info

    def _fetch_album_assets_info(self, album_id: str | None) -> list[dict[str, Any]]:
        """ 指定アルバムのアセット情報を取得する """
        if not album_id:
            return []
        return self.provider.fetch_album_assets(album_id)

    def _fetch_daily_pickup_assets_info(self) -> list[dict[str, Any]]:
        """ デイリーピックアップ用のアセット情報を取得する """
        all_albums = self.provider.fetch_albums()
        if not all_albums:
            return []

        all_album_ids = [a['id'] for a in all_albums]
        album_name_map = {a['id']: a['albumName'] for a in all_albums}

        pickup_mgr = DailyPickupManager(self.config)
        selected_album_ids = pickup_mgr.get_today_album_ids(all_album_ids)
        logger.info('デイリーピックアップ対象: %s',
                    [album_name_map.get(aid, aid) for aid in selected_album_ids])

        assets_info: list[dict[str, Any]] = []
        failed = 0
        for album_id in selected_album_ids:
            album_name = album_name_map.get(album_id, '')
            try:
                # 旧実装（移植元）はデイリーピックアップでアルバムの並び順設定を
                # 参照していなかった（単一アルバム選択のみが反映する挙動だった）。
                # 複数アルバムを混ぜて表示する以上ここでの並び順の意味は薄く、
                # 反映すると asc のアルバムだけ逆順になる回帰を招くため据え置く
                items = self.provider.fetch_album_assets(album_id, album_name,
                                                          apply_album_order=False)
            except ProviderError as e:
                # 1つのアルバムが取れなくても残りは表示したいので、ここだけは継続する
                self._update_status(f'アルバム取得エラー (ID: {album_id}): {e}')
                failed += 1
                continue
            assets_info.extend(items)

        if (failed and failed == len(selected_album_ids)
                and getattr(self.provider, 'rescan_on_load', False)):
            # 全アルバムが失敗したのに空リストを返すと、`load_list()` が「走査成功で
            # 空」と誤認する。失敗として伝える（rescan_on_load の取得元だけ。
            # Immich / Drive は従来どおり空リストのまま失効キャッシュへ落ちる）
            raise ProviderError('選択されたアルバムをすべて読み込めませんでした')

        return assets_info

    def ensure_photo(self, asset_id: str) -> Path | None:
        """
        表示解像度で確定済みの写真をディスクに用意し、そのパスを返す。

        **ネットワークアクセスを伴うため、必ずワーカースレッドから呼ぶこと。**
        メインスレッドから呼ぶと描画が止まる。
        """
        path = self.cache.get_photo_path(asset_id)
        if path is not None:
            return path

        raw = self.provider.fetch_photo(asset_id)
        if raw is None:
            return None
        return self.cache.store_photo(asset_id, raw)

    def cached_date(self, asset_id: str) -> str:
        """
        キャッシュ作成時に原本の EXIF から残した撮影日（ISO 文字列。無ければ空文字）。
        写真リストに日付を持たない取得元（S3 等。一覧では原本を開けない）の表示用。
        ファイルを読むため、`ensure_photo()` と同じくワーカースレッドから呼ぶこと。
        """
        return self.cache.get_cached_date(asset_id)
