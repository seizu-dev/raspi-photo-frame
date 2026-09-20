"""
pi-photo-frame アプリケーション本体

Immich から取得した写真をスライドショー表示し、人感センサーとタッチで
省電力制御を行う。ロジック層（src/*.py）と描画層（src/gui/）を組み立て、
メインループを回すのがこのファイルの役割。
"""

import logging
import os
import queue
import signal
import sys
import threading
import time
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('main')

# SDL_VIDEODRIVER / SDL_RENDER_DRIVER は Dockerfile / docker-compose.yml の ENV で
# 与える契約。ここは Dev Container 外で直接実行する場合のフォールバックのみ。
os.environ.setdefault('SDL_VIDEODRIVER', 'kmsdrm')

import pygame as pg  # noqa: E402  (環境変数設定後に import する必要がある)

from src.config_manager import ConfigManager  # noqa: E402
from src.display_manager import DisplayManager  # noqa: E402
from src.gui.overlay import Overlay, read_vmrss_kb  # noqa: E402
from src.gui.renderer import TAP_DOWN, Renderer  # noqa: E402
from src.gui.screens import (  # noqa: E402
    ACTION_ALBUM,
    ACTION_BACK,
    ACTION_MENU,
    ACTION_QUIT,
    ACTION_RELOAD,
    ACTION_SETTINGS,
)
from src.gui.screens.album import AlbumScreen  # noqa: E402
from src.gui.screens.menu import MenuScreen  # noqa: E402
from src.gui.screens.settings import SettingsScreen  # noqa: E402
from src.gui.screens.slideshow import SlideshowScreen  # noqa: E402
from src import i18n  # noqa: E402
from src.i18n import t  # noqa: E402
from src.immich_api import ImmichAPI  # noqa: E402
from src.motion_sensor import MotionSensor  # noqa: E402
from src.photo_cache import PhotoCache  # noqa: E402
from src.photo_source import PhotoSource  # noqa: E402
from src.touch_watcher import TouchWatcher  # noqa: E402

# 写真リストを取得できなかったときの再試行間隔
RETRY_INTERVAL_SEC = 60.0
LOG_INTERVAL_SEC = 5.0
# 日付が変わったかを確認する間隔。date.today() は軽いが毎フレーム呼ぶ必要はなく、
# デイリーピックアップの切り替わりが最大この秒数だけ遅れても実害が無い
DATE_CHECK_INTERVAL_SEC = 60.0

# display_manager.py / motion_sensor.py と同じ式。開発環境（Dev Container / x11）は
# 消灯中に SDL を破棄すると /dev/input も人感センサーのモックも使えず復帰手段が
# 無くなるため、この定数が真のときだけ復帰待ちウィンドウ（renderer の
# poll_wake_window）を使う。`DisplayManager.available` では判定しない。
# `available` は実機でも libdrm の読み込みに失敗すると False になりうるが、
# その場合に KMSDRM で復帰待ちウィンドウを開くと DRM master を握ってしまい、
# 実機の消灯そのものを壊す（「消灯できないだけ」と「勝手に master を奪う」は別問題）
IS_DEV_ENVIRONMENT = os.environ.get('IS_DEV_ENVIRONMENT', 'false').lower() == 'true'


def check_writable(env_name: str, fallback: str) -> None:
    """
    PF_CONFIG_DIR / PF_CACHE_DIR への書き込みテストを起動時に1回だけ行う。

    named volume（/cache）と bind mount（/config）の権限（uid 1000）が通っているかを
    確認する。この構成で一番壊れやすい箇所のため、毎回検証してログに残す。
    """
    directory = os.environ.get(env_name, fallback)
    test_path = os.path.join(directory, '.write_test')
    try:
        os.makedirs(directory, exist_ok=True)
        with open(test_path, 'w', encoding='utf-8') as f:
            f.write('ok')
        os.remove(test_path)
        logger.info('書き込みテスト成功: %s=%s', env_name, directory)
    except OSError as e:
        logger.error('書き込みテスト失敗: %s=%s error=%r', env_name, directory, e)


class App:
    """ アプリケーション全体の組み立てとメインループ """

    def __init__(self) -> None:
        self._should_stop = False
        self._config = ConfigManager()
        # どの画面・オーバーレイを作るより前に言語を確定させる。以降に作る
        # Renderer / Overlay / 各画面のテキストがここで決まった言語で組み立つ
        # （.claude/architecture.md「対で更新が必要な箇所」参照）。
        i18n.set_language(self._config.get('language'))

        self._renderer = Renderer()
        self._renderer.create()
        self._overlay = Overlay(self._renderer, self._config)

        self._cache = PhotoCache(self._config, display_size=self._renderer.size)
        # 起動時に一度だけ上限を強制する（保存 N 回ごとの間引きとは別に）
        self._cache.enforce_limit(force=True)

        self._api: ImmichAPI | None = None
        self._source: PhotoSource | None = None
        try:
            self._api = ImmichAPI(self._config, status_callback=self._overlay.set_status)
            self._source = PhotoSource(self._config, self._api, self._cache)
        except ValueError as e:
            # 資格情報が無くても起動はする。設定を直せば再試行で拾える
            logger.error('Immich に接続できません: %s', e)
            self._overlay.set_status(t('status.no_immich'))

        self._slideshow = SlideshowScreen(self._renderer, self._overlay,
                                          self._config, self._source) if self._source else None

        # 画面スタック。末尾が最前面。基底は常に slideshow で、破棄しない
        # （先読みスレッドと写真テクスチャを維持し続けるため）。
        # Immich の資格情報が無く slideshow を作れない場合はスタックを空のままにする
        # （メニューを開く導線自体が歯車ボタン経由 = slideshow 依存のため、
        # この状態では従来どおりメニューへ到達できない。既存の制約であり本ステップの範囲外）。
        self._menu_screen = MenuScreen(self._renderer)
        self._settings_screen = SettingsScreen(self._renderer, self._config,
                                               on_changed=self._on_setting_changed)
        # アルバム選択には Immich API が要る。資格情報が無ければ作らず、
        # ACTION_ALBUM のディスパッチ側でステータス表示にフォールバックする
        self._album_screen = (AlbumScreen(self._renderer, self._config, self._api, self._cache)
                              if self._api is not None else None)
        self._screens: list = [self._slideshow] if self._slideshow is not None else []

        # 消灯時は SDL を破棄して DRM master を解放する。順序は DisplayManager が保証する
        self._display = DisplayManager(
            on_release_display=self._renderer.destroy,
            on_recreate_display=self._renderer.create,
            wakeup_delay=float(self._config.get('display_wakeup_delay', 3.0)),
        )

        self._sensor = MotionSensor(on_motion=self._on_activity)
        # start/stop の呼び出しを直列化するロックと、「意図した状態」を保持するフラグ。
        # start() の戻り値（GPIO を実際に確保できたか）とは別物にする。開発環境では
        # 確保に必ず失敗し常に False が返るため、戻り値を採用すると設定が有効なままの
        # 場合に毎ループ start() を呼び直すことになる
        self._sensor_lock = threading.Lock()
        self._sensor_running = False
        self._apply_motion_sensor_setting()

        # 消灯中は SDL が無く pygame のイベントを取れないため、
        # /dev/input を直接読んでタッチでの復帰を可能にする
        self._touch = TouchWatcher(on_input=self._on_activity)
        self._touch.start()

        self._list_queue: queue.Queue = queue.Queue()
        # 写真リスト取得の世代番号。起動時/再試行/ACTION_RELOAD/表示順変更/
        # デイリーピックアップ数変更/日付の切り替わりの6経路が非同期に発火しうるため、
        # 発火のたびに採番して「最新のリクエストの結果だけを採用する」ために使う
        # （後発のリクエストより先に古いリクエストの結果が届いた場合の上書き防止）。
        self._list_request_token = 0
        self._retry_at = 0.0
        # デイリーピックアップは日付でローテーションするため、日付が変わったら
        # 取得し直す必要がある。スライドショーは写真リストを一巡しても再取得せず、
        # 実機は restart: unless-stopped の常時稼働なので、ここで見ないと
        # 再起動するまで初日の選択が表示され続ける
        self._today = date.today()
        self._next_date_check_at = 0.0
        self._last_activity = time.monotonic()
        # 復帰待ちなどで入力を捨てたことを覚えておく印。
        # 真の間は次の TAP_DOWN まで受け付けない（下の _handle_events を参照）
        self._awaiting_down = False

        self._frames = 0
        self._frames_since_log = 0
        self._last_log = time.monotonic()

    # ------------------------------------------------------------------ 補助処理

    def _on_activity(self) -> None:
        """ 人感センサー / 入力監視のコールバック。ワーカースレッドから呼ばれる """
        self._last_activity = time.monotonic()

    def _apply_motion_sensor_setting(self) -> None:
        """
        `motion_sensor_enabled` の現在値に合わせてセンサーの start/stop を行う。

        呼び出し時点の設定値を読み直すため、設定変更の反映と起動時の初期化の
        両方から共通で使える。stop() はスレッドの join で最大2秒かかりうるため、
        呼び出し側でメインループを止めない工夫（ワーカースレッド化）をすること。
        """
        with self._sensor_lock:
            want = bool(self._config.get('motion_sensor_enabled', True))
            if want == self._sensor_running:
                return
            if want:
                self._sensor.start()
                self._sensor_running = True
            else:
                self._sensor.stop()
                self._sensor_running = False

    def _on_setting_changed(self, key: str) -> None:
        """
        基本設定画面からのコールバック。

        画面自身は他のコンポーネントを直接触らない設計にしてあるため、
        キーごとの反映処理はここに集約する
        （`.claude/plans/delegated-leaping-perlis.md` ステップ2の一覧）。
        値は既に `ConfigManager.set()` 済みなので、ここでは反映だけを行う。
        """
        if key in ('interval', 'transition_duration'):
            # スライダーで秒数を変えても次の自動送りまでの残り時間が古いままだと
            # 変更が体感できない。activity を更新してタイマーを引き直す
            if self._slideshow is not None:
                self._slideshow.notify_activity()

        elif key == 'display_mode':
            # sequential/random の切り替えは並び順そのものが変わるため取得し直す
            self._load_photos_async(force=True)

        elif key == 'photo_fit':
            # キャッシュのファイル名が方式ごとに変わるため、今表示している写真を
            # 新しい方式で取り直す（放置すると一巡するまで見た目が変わらない）
            if self._slideshow is not None:
                self._slideshow.reload_current()

        elif key == 'daily_pickup_count':
            # デイリーピックアップを見ているときだけ選び直す。他ソースを見ている間に
            # 数だけ変えても、次にデイリーピックアップへ切り替えたときに効けばよい
            if self._config.get('source') == 'daily_pickup':
                self._config.set('daily_pickup_date', '')
                self._load_photos_async(force=True)

        elif key in ('comment_font_size', 'show_comment', 'show_clock', 'show_memory_usage'):
            # オーバーレイの保持テクスチャを作り直させる。ここでは呼ぶだけで、
            # 実際の再生成は次の Overlay.update() で行われる（毎フレーム作り直さない契約）
            self._overlay.invalidate()

        elif key == 'photo_cache_max_mb':
            # 数百ファイルの走査になりうるため、メインループを止めないようワーカーへ逃がす
            def worker() -> None:
                self._cache.enforce_limit(force=True)

            threading.Thread(target=worker, name='cache-enforce-limit', daemon=True).start()

        elif key == 'display_wakeup_delay':
            self._display.wakeup_delay = float(self._config.get('display_wakeup_delay', 3.0))

        elif key == 'motion_sensor_enabled':
            # stop() はスレッドの join で最大2秒かかりうるため、メインループを
            # 止めないようワーカーへ逃がす
            def worker() -> None:
                self._apply_motion_sensor_setting()

            threading.Thread(target=worker, name='motion-sensor-toggle', daemon=True).start()

        elif key == 'language':
            # 3画面（menu/settings/album）は i18n.generation() を見て自前で
            # _recreate_if_needed() から追従するが、オーバーレイはそこに相乗り
            # していないため invalidate() を明示的に呼ばないと、写真が変わるまで
            # 古い言語のテクスチャ（時計・カウンタ・説明文・日付）を使い続ける
            i18n.set_language(self._config.get('language'))
            self._overlay.invalidate()

        elif key in ('date_format', 'time_format'):
            # 写真の日付テクスチャは「写真が変わったとき」しか作り直されないため、
            # date_format の変更は invalidate() を呼ばないと反映されない。
            # time_format は時計が CLOCK_INTERVAL（1秒）で常に作り直されるので
            # 呼ばなくても直るが、date_format と挙動を揃えるために同じく呼ぶ
            self._overlay.invalidate()

        # power_saving_enabled / power_saving_timeout / show_countdown はメインループが
        # 毎回 config を読みに行くため、ここでの配線は不要
        # （SettingsScreen 側で ConfigManager.set() 済みであれば次のループから効く）

    def _check_date_rollover(self, now: float) -> None:
        """
        日付が変わったらデイリーピックアップの写真リストを取得し直す。

        点灯中・消灯中のどちらでも回す。日付が変わる深夜は通常消灯しており、
        そこで写真リストの取得（Immich への問い合わせ。実測 2.4 秒）を済ませておくと、
        復帰した時点で新しいリストがすぐ適用される。**写真本体のキャッシュ充填は
        復帰後**になる（先読みは `set_photos()` 起点で、それを呼ぶ
        `_collect_photo_list()` は消灯中の continue より後ろにあるため）。
        消灯中に呼んでも安全なのは、
        `_load_photos_async()` が触るのがステータス文字列とワーカースレッドだけで
        SDL に触れないため（結果の適用は復帰後の `_collect_photo_list()` で行われる）。
        """
        if now < self._next_date_check_at:
            return
        self._next_date_check_at = now + DATE_CHECK_INTERVAL_SEC

        today = date.today()
        if today == self._today:
            return

        previous, self._today = self._today, today

        # 他のソースを見ている間に日を跨いだ場合も記録だけは進める。次にデイリー
        # ピックアップへ切り替えるときはアルバム選択画面の ACTION_RELOAD が
        # 取得を起こすため、ここで取りこぼしにはならない
        if self._config.get('source') != 'daily_pickup':
            return

        logger.info('日付が変わりました（%s -> %s）。デイリーピックアップを取得し直します',
                    previous, today)
        # force は付けない。cache_key が daily_pickup_<日付> なので新しい日は
        # キャッシュに存在せず、そのまま API を叩く。daily_pickup_date を空にする
        # 操作も不要で、DailyPickupManager が日付差を見て次の N 件を取り出す
        # （空にすると同じ日のまま選び直す挙動になり、ローテーションのキューを
        # 余計に消費してしまう）
        self._load_photos_async()

    def _load_photos_async(self, force: bool = False) -> None:
        if self._source is None:
            return

        # このリクエストの世代を採番する。ワーカーは結果と一緒にこの番号を積み、
        # 取り出し側（_collect_photo_list）が最新の番号と一致するものだけを採用する。
        self._list_request_token += 1
        token = self._list_request_token

        def worker() -> None:
            self._list_queue.put((token, self._source.load_list(force=force)))

        threading.Thread(target=worker, name='photo-list', daemon=True).start()
        self._overlay.set_status(t('status.loading_photos'))

    def _collect_photo_list(self, now: float) -> None:
        # 1フレームの間に複数の結果が溜まっている可能性があるため、
        # 空になるまで取り出して最後に受け取ったものだけを見る
        # （古い結果はここで自然に捨てられる。取り出さず1件だけ見ると、
        # 同じフレームに古い結果と新しい結果が並んでいた場合に古い方を
        # 拾ってしまいうる）。
        latest = None
        while True:
            try:
                latest = self._list_queue.get_nowait()
            except queue.Empty:
                break

        if latest is None:
            return
        token, photos = latest

        if token != self._list_request_token:
            # このフレームで取り出した最新の結果ですら、その後にさらに新しい
            # リクエストが発火済みだった。古い結果は破棄する
            logger.info('写真リスト取得結果を破棄しました（古いリクエスト token=%d, 最新=%d）',
                        token, self._list_request_token)
            return

        if photos:
            self._overlay.set_status(t('status.slideshow_start', count=len(photos)))
            self._slideshow.set_photos(photos)
            self._retry_at = 0.0
        else:
            self._overlay.set_status(
                t('status.no_photos', seconds=int(RETRY_INTERVAL_SEC)))
            self._retry_at = now + RETRY_INTERVAL_SEC

    # ------------------------------------------------------------ 画面スタック

    def _top_screen(self):
        return self._screens[-1] if self._screens else None

    def _push_screen(self, screen) -> None:
        """ 画面を1つ上に重ねる。既に最前面ならそのまま何もしない """
        if self._screens and self._screens[-1] is screen:
            return
        if self._screens:
            self._screens[-1].on_leave()
        self._screens.append(screen)
        screen.on_enter()

    def _pop_screen(self) -> None:
        """ 最前面の画面を1つ閉じる。基底の slideshow は閉じない """
        if len(self._screens) <= 1:
            return
        leaving = self._screens.pop()
        leaving.on_leave()
        self._screens[-1].on_enter()

    def _dispatch_action(self, action: str | None) -> None:
        """ 画面の handle_input が返したアクション定数をルーティングする """
        if action is None:
            return
        if action == ACTION_MENU:
            self._push_screen(self._menu_screen)
        elif action == ACTION_BACK:
            self._pop_screen()
        elif action == ACTION_SETTINGS:
            self._push_screen(self._settings_screen)
        elif action == ACTION_ALBUM:
            if self._album_screen is not None:
                self._push_screen(self._album_screen)
            else:
                self._overlay.set_status(t('status.album_needs_immich'))
        elif action == ACTION_QUIT:
            logger.info('メニューから終了が要求されました')
            self._should_stop = True
        elif action == ACTION_RELOAD:
            # アルバム選択画面の「決定」から来る。source/album は既に保存済みなので、
            # 画面を1つ閉じてメニューへ戻したうえで写真リストを取得し直す
            self._pop_screen()
            self._load_photos_async(force=True)
        else:
            logger.warning('未知のアクションです: %s', action)

    # -------------------------------------------------------------------- 入力

    def _handle_events(self, now: float) -> None:
        events = pg.event.get()
        for event in events:
            if event.type == pg.QUIT:
                self._should_stop = True

        top_screen = self._top_screen()

        for kind, x, y in self._renderer.normalize_events(events):
            self._last_activity = now

            if not self._display.is_on:
                # 消灯中のタップは点灯だけして消費する。誤操作で写真が送られないように
                self._display.turn_on()
                self._awaiting_down = True
                continue

            if not self._display.is_ready:
                # パネルはまだ映っていない（実測で約 2.0〜2.4 秒かかる）。
                # この時間帯の操作は誤操作にしかならないので、復帰のきっかけに
                # なった指の FINGERUP も、待ちきれずに触られた分もすべて捨てる。
                # **1つだけ捨てる方式では復帰待ちの連打を止められなかった。**
                self._awaiting_down = True
                continue

            if self._awaiting_down:
                # 捨てた入力の続きが届いている。DOWN と対になっていない UP を
                # 通すと、スライドショーではそのまま写真送りになる
                # （handle_tap は TAP_UP だけで判定するため）。次の DOWN から拾う
                if kind != TAP_DOWN:
                    continue
                self._awaiting_down = False

            if top_screen is None:
                continue
            self._dispatch_action(top_screen.handle_input(kind, x, y))

    def _poll_dev_wake_window(self, now: float) -> None:
        """
        開発環境の消灯中に、復帰待ちウィンドウ（noVNC からのクリック）で復帰させる。

        実機は消灯中も /dev/input と GPIO を直読みできるが、Dev Container の x11 は
        destroy() で SDL ごとウィンドウが消えるため、ここでしか復帰の入口が無い。
        タッチ／人感センサーによる復帰（_update_power_saving 内）と同じ扱いにする:
        activity の更新・ログ・turn_on()・_awaiting_down のセット。呼び出し側の
        ループでは _update_power_saving と両方を毎回呼ぶこと（motion/touch を
        必ず両方消費する契約と同様、こちらも読み忘れないため）。
        """
        wake, quit_requested = self._renderer.poll_wake_window()
        if quit_requested:
            self._should_stop = True
        if wake:
            self._last_activity = now
            logger.info('クリックにより復帰します（開発環境）')
            self._display.turn_on()
            # 実機のタッチ復帰と同様、復帰のきっかけになった入力そのものを
            # 写真送りに使わせない（次の TAP_DOWN まで捨てる）
            self._awaiting_down = True

    def _update_power_saving(self, now: float) -> None:
        # 短絡評価にすると片方しか消費されないため、必ず両方を読む
        motion = self._sensor.consume_motion()
        touched = self._touch.consume_input()

        if motion or touched:
            self._last_activity = now
            if not self._display.is_on:
                logger.info('%sにより復帰します', '人感センサーの検知' if motion else 'タッチ')
                self._display.turn_on()
                # 復帰のきっかけになった指を離したときの FINGERUP が
                # 再生成後の SDL に届くことがある。写真送りに使わせない。
                # 実際に捨てるのは _handle_events（is_ready が偽の間はすべて落とす）
                self._awaiting_down = True
                return

        if not self._config.get('power_saving_enabled', True):
            return

        timeout = float(self._config.get('power_saving_timeout', 300))
        if self._display.is_on and now - self._last_activity >= timeout:
            logger.info('無操作が %.0f 秒続いたため消灯します', timeout)
            self._display.turn_off()

    # ------------------------------------------------------------------ ループ

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info('レンダードライバ一覧: %s',
                    [d.name for d in __import__('pygame._sdl2.video',
                                                fromlist=['get_drivers']).get_drivers()])
        logger.info('起動時 VmRSS=%dkB size=%dx%d', read_vmrss_kb(), *self._renderer.size)
        self._load_photos_async()

        while not self._should_stop:
            now = time.monotonic()

            # 消灯判定より前に置く。消灯中も日付の切り替わりを拾うため
            self._check_date_rollover(now)

            if not self._display.is_on:
                # **消灯中は SDL を破棄しているためイベントを取得できない。**
                # pg.event.get() を呼ぶと video system not initialized で落ちる。
                # 実機での復帰の契機はここでは人感センサーのみ（_update_power_saving
                # 内で処理）。開発環境だけ、復帰待ちウィンドウのクリックも見る
                # （/dev/input も人感センサーのモックも無く、他に復帰手段が無いため）。
                if IS_DEV_ENVIRONMENT:
                    self._poll_dev_wake_window(now)
                self._update_power_saving(now)
                time.sleep(0.1)
                continue

            self._handle_events(now)
            self._update_power_saving(now)

            if not self._display.is_on:
                # このループの中で消灯した。SDL は既に破棄されているので描画へ進まない
                continue

            if self._retry_at and now >= self._retry_at:
                self._retry_at = 0.0
                self._load_photos_async(force=True)

            self._collect_photo_list(now)

            top_screen = self._top_screen()
            is_slideshow_top = top_screen is self._slideshow

            # 復帰直後はパネルがまだ映っていない。写真を送らずに待つ。
            # メニュー階層を開いている間も自動送りは止める（allow_advance）が、
            # 先読み結果の取り込みは止めない（テクスチャの世代追従も含むため）。
            ready = self._display.is_ready
            if self._slideshow is not None:
                self._slideshow.update(now, allow_advance=ready and is_slideshow_top)
            if top_screen is not None and not is_slideshow_top:
                top_screen.update(now)
            self._overlay.update(now)

            self._renderer.clear()
            if top_screen is not None:
                top_screen.draw()
            if is_slideshow_top or top_screen is None:
                # メニュー階層では最前面の画面が背景から自分で塗るため、
                # スライドショー用のオーバーレイ（時計・カウンタ・歯車）は重ねない。
                # ただし top_screen が無い（Immich 未設定で slideshow を作れず
                # 画面スタックが空の）場合は、起動時トースト（set_status で出した
                # 「Immich の設定がありません」等）を表示する手段がオーバーレイしか
                # 無いため、この場合だけは描く（変更前の「無条件で描く」挙動へ戻す）。
                self._overlay.draw()
            self._renderer.present()

            self._frames += 1
            self._frames_since_log += 1
            self._log_periodically(now)

        self._shutdown()

    def _log_periodically(self, now: float) -> None:
        if now - self._last_log < LOG_INTERVAL_SEC:
            return
        fps = self._frames_since_log / (now - self._last_log)
        logger.info('fps=%.1f VmRSS=%dkB photos=%d frames=%d',
                    fps, read_vmrss_kb(),
                    self._slideshow.photo_count if self._slideshow else 0,
                    self._frames)
        self._last_log = now
        self._frames_since_log = 0

    def _handle_signal(self, signum, _frame) -> None:
        logger.info('シグナル受信 (%s): 終了処理へ移行します', signal.Signals(signum).name)
        self._should_stop = True

    def _shutdown(self) -> None:
        logger.info('終了処理を開始します（frames=%d）', self._frames)
        if self._slideshow is not None:
            self._slideshow.stop()
        self._touch.stop()
        self._sensor.stop()
        # 消灯中に終了しても画面は点けてから master を手放す
        self._display.cleanup()
        self._renderer.destroy()
        pg.quit()
        logger.info('終了しました')


def main() -> None:
    check_writable('PF_CONFIG_DIR', './config')
    check_writable('PF_CACHE_DIR', './cache')
    App().run()


if __name__ == '__main__':
    main()
    sys.exit(0)
