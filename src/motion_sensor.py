import logging
import os
import threading
import time
from datetime import timedelta
from typing import Callable

logger = logging.getLogger(__name__)

# 開発環境（Dev Container）では GPIO を掴まずモックで動く。イメージの ENV で与えられる。
IS_RASPBERRY_PI = os.environ.get('IS_DEV_ENVIRONMENT', 'false').lower() != 'true'

DEFAULT_CHIP_PATH = '/dev/gpiochip0'
DEFAULT_SENSOR_PIN = 18
CONSUMER_NAME = 'pi-photo-frame'

# AM312 は検知すると数秒 High を保つため、立ち上がりだけを拾えばよい。
# 短いチャタリングはカーネル側のデバウンスで落とす。
DEBOUNCE_MS = 50

# stop() から確実に抜けるため、イベント待ちはタイムアウト付きにする
WAIT_TIMEOUT_SEC = 0.5

if IS_RASPBERRY_PI:
    import gpiod
    from gpiod.line import Bias, Edge
else:
    gpiod = None


class MotionSensor:
    """
    AM312 PIR 人感センサーを監視するクラス

    photo-frame は lgpio と kivy.clock.Clock を使っていたが、どちらも使えないため
    libgpiod v2（`gpiod`）とバックグラウンドスレッドで作り直した。
    lgpio には cp313 aarch64 の wheel が無く、Dockerfile に builder ステージが
    必要になるのを避ける狙いもある。

    検知の通知は2経路ある。コールバックと、`consume_motion()` によるポーリングである。
    **`pygame.event.post()` は使わない。** 消灯中は SDL を破棄して DRM master を
    解放する設計のため、SDL の状態に依存すると消灯中の検知を取りこぼす。
    """

    def __init__(self, sensor_pin: int = DEFAULT_SENSOR_PIN,
                 chip_path: str = DEFAULT_CHIP_PATH,
                 on_motion: Callable[[], None] | None = None) -> None:
        self._pin = sensor_pin
        self._chip_path = chip_path
        self._on_motion = on_motion

        self._request = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # 監視スレッドとメインループの両方から触るため、状態は必ずロックで守る
        self._lock = threading.Lock()
        self._last_motion_time: float | None = None
        self._pending = False

    @property
    def available(self) -> bool:
        """ 実際に GPIO を掴めているか。開発環境では常に False """
        return self._request is not None

    @property
    def last_motion_time(self) -> float | None:
        """ 最後に検知した時刻（time.monotonic ベース）。未検知なら None """
        with self._lock:
            return self._last_motion_time

    def start(self) -> bool:
        """
        GPIO を確保して監視スレッドを開始する。

        戻り値は「実際に GPIO の監視を始められたか」。開発環境や GPIO の確保に
        失敗した場合は False を返すが、例外は投げない。人感センサーが使えなくても
        スライドショー自体は動かし続けたいためである。
        """
        if self._thread is not None:
            logger.warning('人感センサーは既に開始しています')
            return self.available

        if not IS_RASPBERRY_PI:
            logger.info('開発環境のため GPIO を確保しません（pin=%d）。'
                        'trigger() で手動発火できます', self._pin)
            return False

        try:
            self._request = gpiod.request_lines(
                self._chip_path,
                consumer=CONSUMER_NAME,
                config={self._pin: gpiod.LineSettings(
                    edge_detection=Edge.RISING,
                    # 未接続時に入力がフローティングして誤検知するのを避ける
                    bias=Bias.PULL_DOWN,
                    debounce_period=timedelta(milliseconds=DEBOUNCE_MS),
                )},
            )
        except (OSError, ValueError) as e:
            # GID 不足やデバイス未パススルーでもアプリは動かす
            logger.error('人感センサーの GPIO を確保できませんでした（chip=%s pin=%d）: %s',
                         self._chip_path, self._pin, e)
            self._request = None
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name='motion-sensor', daemon=True)
        self._thread.start()
        logger.info('人感センサーの監視を開始しました（chip=%s pin=%d）', self._chip_path, self._pin)
        return True

    def stop(self) -> None:
        """ 監視スレッドを止めて GPIO を解放する。多重に呼んでも安全 """
        self._stop_event.set()

        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=WAIT_TIMEOUT_SEC * 4)
            if thread.is_alive():
                logger.warning('人感センサーの監視スレッドが停止しませんでした')

        if self._request is not None:
            try:
                self._request.release()
            except (OSError, RuntimeError) as e:
                logger.warning('人感センサーの GPIO を解放できませんでした: %s', e)
            self._request = None
            logger.info('人感センサーを停止しました')

    def consume_motion(self) -> bool:
        """
        前回の呼び出し以降に検知があったかを返し、フラグを消費する。

        メインループから毎フレーム呼ぶ想定。ログは検知時のみ出るのでここでは出さない。
        """
        with self._lock:
            pending, self._pending = self._pending, False
        return pending

    def trigger(self) -> None:
        """ 検知を手動で発火させる。開発環境での動作確認用 """
        self._notify()

    def _run(self) -> None:
        """ 監視スレッド本体。タイムアウト付きで待ち、stop() に反応できるようにする """
        while not self._stop_event.is_set():
            try:
                if not self._request.wait_edge_events(WAIT_TIMEOUT_SEC):
                    continue
                events = self._request.read_edge_events()
            except (OSError, RuntimeError) as e:
                if self._stop_event.is_set():
                    break
                logger.error('人感センサーの読み取りに失敗したため監視を終了します: %s', e)
                break

            for _ in events:
                self._notify()

    def _notify(self) -> None:
        with self._lock:
            self._last_motion_time = time.monotonic()
            self._pending = True

        logger.info('人感センサーが動きを検知しました')
        if self._on_motion is None:
            return
        try:
            self._on_motion()
        except Exception:
            # コールバック側の例外で監視スレッドを止めない。握らずログには必ず残す
            logger.exception('人感センサーのコールバックで例外が発生しました')
