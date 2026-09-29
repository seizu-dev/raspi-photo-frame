"""
写真取得元（provider）の抽象化層

Immich 以外の取得元（Google Drive 等。PR2 以降）を将来追加できるようにするための
境界。PR1 時点では Immich のみを実装しており、`ImmichAPI` が `PhotoProvider`
Protocol を満たす形になっている（.claude/plans/abundant-weaving-kernighan.md PR1）。

このモジュールは `src.immich_api` を **モジュールレベルでは import しない**
（`create_provider()` の中でだけ遅延 import する）。`immich_api.py` は
`ProviderError` をこちらから import するため、モジュールレベルで相互 import すると
循環importになる。
"""

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from src.config_manager import ConfigManager

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """
    取得元の境界で発生した通信・認証エラーを表す例外。

    `requests` 等、取得元の実装に依存する例外をここに変換してから送出する。
    呼び出し側（`photo_source.py`）は取得元の実装を知らずに済む。
    """


class PhotoProvider(Protocol):
    """
    写真取得元が満たすべき契約。

    - `name`: 'immich' | 'gdrive' | （将来）'local'。ログや `active_provider`
      （実行時状態。`config_manager.py` の既定値）に使う短い識別子
    - `cache_namespace`: `PhotoCache` がサブディレクトリを分けるための名前。
      Immich は既存キャッシュを温存するため空文字（`PhotoCache` は名前空間なしと
      同じ場所を使う）。空文字以外の取得元は `photos/<ns>/` 等へ分離される
    - `supports_favorites`: False の取得元では、アルバム選択画面の
      お気に入り仮想エントリを表示しない（`reconcile_settings()` も参照）
    - `delivers_originals`: True の取得元は `PhotoCache` 側で EXIF 補正・
      画素上限などの原本向け処理が必要になる（PR2 で実装。PR1 では常に False）
    """

    name: str
    cache_namespace: str
    supports_favorites: bool
    delivers_originals: bool

    def fetch_albums(self) -> list[dict[str, Any]]:
        """ アルバムの一覧を返す（`id` / `albumName` / `albumThumbnailAssetId` を持つ） """
        ...

    def fetch_album_assets(self, album_id: str, album_name: str = '',
                           apply_album_order: bool = True) -> list[dict[str, Any]]:
        """
        指定アルバムのアセット情報を返す。`album_name` を渡すと各アセットに
        `album_name` キーを付与する（デイリーピックアップで複数アルバムを
        混ぜて表示するときの説明文 `[アルバム名]` に使う）。

        `apply_album_order` はアルバムの並び順設定（'asc'/'desc'）を反映するかどうか。
        既定は True（単一アルバム選択の従来どおりの挙動）。デイリーピックアップだけは
        `PhotoSource` が False を渡す。旧実装（移植元）がデイリーピックアップでは
        アルバムの `order` を一切見ておらず、PR1 でこの窓口へ一本化した際に
        並び順を反映してしまうと asc のアルバムが逆順になる回帰が起きたため、
        旧来の挙動（反映しない）を保つ。
        """
        ...

    def fetch_favorite_assets(self) -> list[dict[str, Any]]:
        """ お気に入りのアセット情報を返す。`supports_favorites` が False の取得元は呼ばれない """
        ...

    def fetch_photo(self, asset_id: str) -> bytes | Path | None:
        """ 写真本体を取得する。表示解像度への変換は行わない（呼び出し側 = PhotoCache の責務） """
        ...

    def fetch_album_thumbnail(self, album: dict[str, Any]) -> bytes | Path | None:
        """ アルバムサムネイルを取得する """
        ...


def create_provider(config: 'ConfigManager',
                    status_callback: Callable[[str], None] | None = None) -> PhotoProvider:
    """
    環境変数 `PF_PHOTO_PROVIDER` に応じて取得元を生成する。

    未設定・空文字は 'immich'（既定）。'gdrive' は PR2 で実装予定のため、
    現時点では分かりやすいメッセージの ValueError を送出する。生成に失敗した
    場合（Immich の資格情報が無い等の ValueError を含む）は呼び出し側
    （main.py）が今までどおり「起動はするがスライドショーを作らない」扱いに
    できるよう、ここでは例外を握りつぶさずそのまま伝播させる。
    """
    provider_name = (os.environ.get('PF_PHOTO_PROVIDER') or 'immich').strip().lower()

    if provider_name == 'immich':
        # モジュールレベルで import すると immich_api.py -> photo_provider.py ->
        # immich_api.py の循環importになるため、ここでだけ遅延 import する。
        from src.immich_api import ImmichAPI
        return ImmichAPI(config, status_callback=status_callback)

    if provider_name == 'gdrive':
        raise ValueError(
            'PF_PHOTO_PROVIDER=gdrive はまだ実装されていません（PR2 で追加予定です）。'
            ' immich を指定するか、環境変数を未設定のままにしてください。')

    raise ValueError(f'未知の PF_PHOTO_PROVIDER です: {provider_name!r}')


def reconcile_settings(config: 'ConfigManager', provider: PhotoProvider) -> None:
    """
    起動時に、選択中の取得元と実際に生成された provider の食い違いを整合させる。

    1. `source == 'favorites'` なのに provider がお気に入りに対応していなければ
       `daily_pickup` へ切り替える（アルバム選択画面は `supports_favorites` を見て
       お気に入りの仮想エントリ自体を隠すが、既存の `settings.json` に古い値が
       残っている場合の保険）
    2. 前回稼働していた取得元（`active_provider`）と今回の provider が違う場合、
       `album_id` 等の取得元固有の状態を初期値へ戻す。**`active_provider` が
       空文字（＝既存の実機の settings.json。この状態キーが導入される前）から
       'immich' へ変わる場合は初期化しない**（アップデートのたびに実機の設定が
       消えるのを防ぐため）
    """
    if config.get('source') == 'favorites' and not provider.supports_favorites:
        logger.info('取得元 %s はお気に入りに対応していないため、source を daily_pickup に切り替えます',
                    provider.name)
        config.set('source', 'daily_pickup')

    active_provider = config.get('active_provider', '')
    if active_provider and active_provider != provider.name:
        logger.info('取得元が切り替わりました（%s -> %s）。関連する状態を初期値へ戻します',
                    active_provider, provider.name)
        config.set('album_id', '')
        config.set('album_name', '')
        config.set('daily_pickup_date', '')
        config.set('daily_pickup_selected_ids', [])
        config.set('daily_pickup_remaining_ids', [])

    config.set('active_provider', provider.name)
