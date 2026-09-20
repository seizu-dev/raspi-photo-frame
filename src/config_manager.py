import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 設定ディレクトリの既定値。コンテナでは Dockerfile の ENV が PF_CONFIG_DIR=/config を
# 与えるが、Dev Container（階層1）では未設定のままワークスペース相対で動く契約になっている。
DEFAULT_CONFIG_DIR = './config'
SETTINGS_FILENAME = 'settings.json'


def resolve_config_dir() -> Path:
    """ 設定ディレクトリを解決する。PF_CONFIG_DIR が未設定なら ./config へフォールバックする """
    return Path(os.environ.get('PF_CONFIG_DIR') or DEFAULT_CONFIG_DIR)


class ConfigManager:
    """ アプリの設定を管理するクラス """

    def __init__(self, settings_path: str | Path | None = None) -> None:
        self.settings_path = Path(settings_path) if settings_path else resolve_config_dir() / SETTINGS_FILENAME
        self.defaults: dict[str, Any] = {
            # 表示言語。ja（日本語）/ en（英語）。src/i18n.py の LANGUAGES に対応する。
            "language": "ja",
            # 時計の書式。24h（24時間表記・現行の挙動）/ 12h（12時制）。
            # src/i18n.py の TIME_FORMATS に対応する。
            "time_format": "24h",
            # 写真の撮影日の書式。src/i18n.py の DATE_FORMATS に対応する。
            # 既定 ymd_slash は現行の raw[:10].replace('-', '/')（例: 2024/03/12）と
            # 一字一句同じ結果になる。実機の既存 settings.json に自動補完で
            # このキーが増えても見た目が変わらないよう、この既定値を選んでいる。
            "date_format": "ymd_slash",
            "interval": 10,
            # crossfade / fade_black / slide / wipe / random の5種類を
            # src/gui/transitions.py で実装している。photo-frame では "random" が
            # 既定だったが選択肢が無く、コードからも参照されていない死んだキーだった。
            "transition": "crossfade",
            "transition_duration": 1.0,
            "show_comment": True,
            "show_clock": True,
            "show_countdown": True,
            "display_mode": "sequential",
            # 写真の表示方法。contain（内接・現行の挙動）/ cover（外接）/ smart
            # （縦横比が画面と一致するときだけ cover、それ以外は contain）の3種類を
            # src/photo_cache.py で実装している。既定は現行の見た目を変えない contain。
            # 方式ごとにキャッシュのファイル名が変わるため、切り替えるとその方式の
            # ぶんだけ再ダウンロードが発生する（既存キャッシュは温存される）。
            "photo_fit": "contain",
            "source": "favorites",
            "album_id": "",
            # 説明文欄の [アルバム名] 表示に使う。photo-frame では defaults への
            # 定義が漏れており get('album_name', '') で握り潰されていた。
            "album_name": "",
            "max_slides_in_memory": 3,
            "show_memory_usage": False,
            "cache_lifetime_hours": 24,
            # 写真本体のディスクキャッシュ上限（MB）。0 以下で無制限。
            # 超過分は最後に使われたのが古いものから削除する。
            "photo_cache_max_mb": 512,
            "power_saving_enabled": True,
            "power_saving_timeout": 300,
            # 消灯から復帰したあと、実際にパネルが映るまでの待ち時間（秒）。
            # 実測ではパネルの応答に約 2.0〜2.4 秒かかり、SDL の再生成（0.4〜2.0秒）と
            # 並行して進む。マージンを乗せた既定値。
            "display_wakeup_delay": 3.0,
            # 人感センサー（AM312 PIR）の有効・無効。起動時に無効なら start() を呼ばない。
            # 実行中にこの値を切り替えた場合は stop() / start() で GPIO の解放・再取得も行う。
            # 解放するのは、センサーを外したり別プロセスで観測したりできるようにするため。
            "motion_sensor_enabled": True,
            # コメント表示設定
            "comment_font_size": 24,
            # デイリーピックアップ設定
            # 末尾3キーは設定ではなく永続化された実行時状態。photo-frame の同居構成を
            # そのまま踏襲している（分離は daily_pickup_manager.py の移植時に再検討する）。
            "daily_pickup_count": 3,
            "daily_pickup_date": "",
            "daily_pickup_selected_ids": [],
            "daily_pickup_remaining_ids": [],
        }
        self.settings: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        """ 設定ファイルからアプリの設定を読み込む """
        settings = self.defaults.copy()
        if self.settings_path.exists():
            try:
                with open(self.settings_path, 'r', encoding='utf-8') as f:
                    loaded_settings = json.load(f)
                settings.update(loaded_settings)
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                # 壊れた設定ファイルでアプリが起動不能になるのは避けるが、握りつぶさず必ず残す。
                # 実機ではコンソールログが唯一の調査手段になるため。
                logger.warning('設定ファイルの読み込みに失敗したためデフォルト値を使用します: %s (%s)',
                               self.settings_path, e)
        else:
            logger.info('設定ファイルが存在しないためデフォルト値で作成します: %s', self.settings_path)

        # 念のため、不足しているキーをデフォルト値で埋める
        for key, value in self.defaults.items():
            settings.setdefault(key, value)

        self.save(settings)  # 不足キーを補った状態で保存し直す
        return settings

    def get(self, key: str, default: Any = None) -> Any:
        """ 設定値を取得する """
        return self.settings.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """ 設定値を設定する """
        self.settings[key] = value
        self.save(self.settings)

    def save(self, settings_data: dict[str, Any]) -> None:
        """ 現在の設定をファイルに保存する """
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.settings_path, 'w', encoding='utf-8') as f:
            json.dump(settings_data, f, indent=4, ensure_ascii=False)
