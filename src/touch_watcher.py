import glob
import logging
import os
import select
import struct
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_DEVICE_GLOB = '/dev/input/event*'

# Linux の struct input_event（64bit）:
#   struct timeval time (16) + __u16 type (2) + __u16 code (2) + __s32 value (4) = 24
_EVENT = struct.Struct('llHHi')
EVENT_SIZE = _EVENT.size

EV_KEY = 0x01
EV_ABS = 0x03

# poll のタイムアウト。stop() から確実に抜けるため
POLL_TIMEOUT_MS = 500


class TouchWatcher:
    """
    `/dev/input/event*` を直接読んで入力の有無だけを監視するクラス

    **消灯中は SDL を破棄しているため pygame のイベントを取得できない。**
    タッチで画面を復帰させるにはカーネルの input デバイスを直接読むしかない。
    photo-frame は同じ目的で evdev を使っていたが、evdev 2.0.0 は
    ソース配布のみで cp313 aarch64 wheel が無く、Dockerfile に builder ステージが
    必要になる。ここは 24 バイトの構造体を読むだけなので標準ライブラリで足りる。

    座標は解釈しない。「何か触られたか」だけを返す。SDL が生きている間の
    座標付きの入力は pygame 側で受け取る。
    """

    def __init__(self, on_input: Callable[[], None] | None = None,
                 device_glob: str = DEFAULT_DEVICE_GLOB) -> None:
        self._on_input = on_input
        self._device_glob = device_glob

        self._fds: list[int] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        self._lock = threading.Lock()
        self._last_input_time: float | None = None
        self._pending = False

    @property
    def available(self) -> bool:
        """ 監視できる入力デバイスを掴めているか """
        return bool(self._fds)

    @property
    def last_input_time(self) -> float | None:
        with self._lock:
            return self._last_input_time

    def start(self) -> bool:
        """
        入力デバイスを開いて監視スレッドを開始する。

        1つも開けなくても例外は投げない。タッチで復帰できなくなるだけで、
        スライドショー自体は動かし続ける。
        """
        if self._thread is not None:
            logger.warning('入力監視は既に開始しています')
            return self.available

        for path in sorted(glob.glob(self._device_glob)):
            try:
                # 読むだけ。ノンブロッキングで開き、poll で待つ
                self._fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            except OSError as e:
                logger.debug('入力デバイスを開けませんでした: %s (%s)', path, e)

        if not self._fds:
            logger.warning('監視できる入力デバイスがありません（%s）。'
                           '消灯中のタッチでは復帰できません', self._device_glob)
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name='touch-watcher', daemon=True)
        self._thread.start()
        logger.info('入力監視を開始しました（%d デバイス）', len(self._fds))
        return True

    def stop(self) -> None:
        """ 監視を止めて fd を閉じる。多重に呼んでも安全 """
        self._stop_event.set()

        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=POLL_TIMEOUT_MS / 1000 * 4)
            if thread.is_alive():
                logger.warning('入力監視スレッドが停止しませんでした')

        for fd in self._fds:
            try:
                os.close(fd)
            except OSError as e:
                logger.debug('入力デバイスの fd を閉じられませんでした: %s', e)
        if self._fds:
            logger.info('入力監視を停止しました')
        self._fds = []

    def consume_input(self) -> bool:
        """ 前回の呼び出し以降に入力があったかを返し、フラグを消費する """
        with self._lock:
            pending, self._pending = self._pending, False
        return pending

    def _run(self) -> None:
        poller = select.poll()
        for fd in self._fds:
            poller.register(fd, select.POLLIN)

        while not self._stop_event.is_set():
            try:
                ready = poller.poll(POLL_TIMEOUT_MS)
            except OSError as e:
                if self._stop_event.is_set():
                    break
                logger.error('入力の待機に失敗したため監視を終了します: %s', e)
                break

            for fd, _events in ready:
                if self._read_events(fd):
                    self._notify()

    def _read_events(self, fd: int) -> bool:
        """ 1つの fd から読めるだけ読み、実入力が含まれていたら True """
        try:
            data = os.read(fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return False
        except OSError as e:
            logger.debug('入力デバイスの読み取りに失敗しました: %s', e)
            return False

        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _sec, _usec, etype, _code, _value = _EVENT.unpack_from(data, offset)
            # EV_SYN は同期用で毎回流れるため、実入力とはみなさない
            if etype in (EV_KEY, EV_ABS):
                return True
        return False

    def _notify(self) -> None:
        with self._lock:
            self._last_input_time = time.monotonic()
            self._pending = True

        if self._on_input is None:
            return
        try:
            self._on_input()
        except Exception:
            logger.exception('入力監視のコールバックで例外が発生しました')
