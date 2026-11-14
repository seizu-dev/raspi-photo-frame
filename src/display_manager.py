import ctypes as C
import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 開発環境（Dev Container）は x11 で DRM を扱えないため、状態遷移だけを模倣する
IS_RASPBERRY_PI = os.environ.get('IS_DEV_ENVIRONMENT', 'false').lower() != 'true'

DEFAULT_CARD_PATH = '/dev/dri/card0'

# 消灯手段が使えないとき（available が False）のログ文言。
# 実機では libdrm の読み込み失敗や GPIO の確保失敗でもここを通るため、開発環境と分ける
_NO_CONTROL_REASON = ('開発環境のため実際の電源制御は行いません' if not IS_RASPBERRY_PI
                      else '消灯手段が使えないため実際の電源制御は行いません')

# 消灯方式。環境変数 PF_DISPLAY_POWER で main.py が選ぶ
METHOD_DRM_DPMS = 'drm_dpms'
METHOD_GPIO_BACKLIGHT = 'gpio_backlight'
DISPLAY_POWER_METHODS = (METHOD_DRM_DPMS, METHOD_GPIO_BACKLIGHT)

# gpio_backlight で使う GPIO（1号機の HDMI パネルのバックライト）。High=点灯、Low=消灯
DEFAULT_BACKLIGHT_PIN = 26
DEFAULT_GPIO_CHIP_PATH = '/dev/gpiochip0'
GPIO_CONSUMER_NAME = 'pi-photo-frame'

# 復帰してから実際にパネルが映るまでの待ち時間（秒）。
# 実機の実測では DPMS=On 送出からパネルが映るまで約 2.0〜2.4 秒かかった。
# SDL の再生成（0.4〜2.0秒）は並行して進むため、遅い方であるパネル応答が支配する。
# 実測上限にマージンを乗せた値を既定とし、settings.json で調整できる。
DEFAULT_WAKEUP_DELAY = 3.0

# DPMS プロパティの値
DPMS_ON = 0
DPMS_STANDBY = 1
DPMS_SUSPEND = 2
DPMS_OFF = 3

DRM_MODE_CONNECTED = 1


class _DrmModeRes(C.Structure):
    _fields_ = [('count_fbs', C.c_int), ('fbs', C.POINTER(C.c_uint32)),
                ('count_crtcs', C.c_int), ('crtcs', C.POINTER(C.c_uint32)),
                ('count_connectors', C.c_int), ('connectors', C.POINTER(C.c_uint32)),
                ('count_encoders', C.c_int), ('encoders', C.POINTER(C.c_uint32)),
                ('min_width', C.c_uint32), ('max_width', C.c_uint32),
                ('min_height', C.c_uint32), ('max_height', C.c_uint32)]


class _DrmModeConnector(C.Structure):
    _fields_ = [('connector_id', C.c_uint32), ('encoder_id', C.c_uint32),
                ('connector_type', C.c_uint32), ('connector_type_id', C.c_uint32),
                ('connection', C.c_int), ('mmWidth', C.c_uint32), ('mmHeight', C.c_uint32),
                ('subpixel', C.c_int), ('count_modes', C.c_int),
                ('modes', C.c_void_p), ('count_props', C.c_int),
                ('props', C.POINTER(C.c_uint32)), ('prop_values', C.POINTER(C.c_uint64)),
                ('count_encoders', C.c_int), ('encoders', C.POINTER(C.c_uint32))]


class _DrmModeProperty(C.Structure):
    _fields_ = [('prop_id', C.c_uint32), ('flags', C.c_uint32), ('name', C.c_char * 32),
                ('count_values', C.c_int), ('values', C.POINTER(C.c_uint64)),
                ('count_enums', C.c_int), ('enums', C.c_void_p),
                ('count_blobs', C.c_int), ('blob_ids', C.POINTER(C.c_uint32)),
                ('blob_values', C.POINTER(C.c_uint32))]


def _load_libdrm() -> C.CDLL | None:
    """
    libdrm.so.2 を読み込んで argtypes を設定する。

    **argtypes は必ず明示する。** drmModeConnectorSetProperty の第4引数は uint64_t で、
    省略すると値が正しく渡る保証がない（検証スクリプトは restype しか設定していなかった）。
    """
    try:
        lib = C.CDLL('libdrm.so.2', use_errno=True)
    except OSError as e:
        logger.error('libdrm.so.2 を読み込めませんでした: %s', e)
        return None

    lib.drmModeGetResources.argtypes = [C.c_int]
    lib.drmModeGetResources.restype = C.POINTER(_DrmModeRes)
    lib.drmModeFreeResources.argtypes = [C.POINTER(_DrmModeRes)]
    lib.drmModeFreeResources.restype = None

    lib.drmModeGetConnector.argtypes = [C.c_int, C.c_uint32]
    lib.drmModeGetConnector.restype = C.POINTER(_DrmModeConnector)
    lib.drmModeFreeConnector.argtypes = [C.POINTER(_DrmModeConnector)]
    lib.drmModeFreeConnector.restype = None

    lib.drmModeGetProperty.argtypes = [C.c_int, C.c_uint32]
    lib.drmModeGetProperty.restype = C.POINTER(_DrmModeProperty)
    lib.drmModeFreeProperty.argtypes = [C.POINTER(_DrmModeProperty)]
    lib.drmModeFreeProperty.restype = None

    lib.drmModeConnectorSetProperty.argtypes = [C.c_int, C.c_uint32, C.c_uint32, C.c_uint64]
    lib.drmModeConnectorSetProperty.restype = C.c_int

    lib.drmSetMaster.argtypes = [C.c_int]
    lib.drmSetMaster.restype = C.c_int
    lib.drmDropMaster.argtypes = [C.c_int]
    lib.drmDropMaster.restype = C.c_int
    return lib


class _DpmsBackend:
    """
    DRM DPMS で消灯するバックエンド（2号機: HDMI + Full KMS）。

    **DRM master は1プロセスしか保持できない。** SDL が master を握ったまま
    別 fd から drmSetMaster すると EACCES で拒否される。そのため `off()` は
    SDL の破棄が済んだ後に呼ばれる前提で、master を取って DPMS=Off にする。
    消灯中は fd と master を保持し続ける（他プロセスに画面を触らせないため）。
    """

    # 復帰は DPMS=On -> drmDropMaster の後に SDL を再生成する（master を返さないと SDL が取れない）
    restore_after_recreate = False

    def __init__(self, card_path: str) -> None:
        self._card_path = card_path
        self._lib = _load_libdrm() if IS_RASPBERRY_PI else None
        self._fd: int | None = None

        if IS_RASPBERRY_PI and self._lib is None:
            logger.warning('libdrm を使えないため画面の消灯は無効になります')

    @property
    def available(self) -> bool:
        """ 実際に DRM で消灯できるか。開発環境では常に False """
        return IS_RASPBERRY_PI and self._lib is not None

    def off(self) -> bool:
        """ master を取って DPMS=Off にする。失敗時は自分の確保物を戻して False """
        try:
            self._fd = os.open(self._card_path, os.O_RDWR)
        except OSError as e:
            logger.error('DRM デバイスを開けませんでした（%s）: %s', self._card_path, e)
            return False

        rc = self._lib.drmSetMaster(self._fd)
        if rc != 0:
            # SDL の破棄が済んでいれば通るはず。EACCES ならまだ誰かが master を握っている
            logger.error('drmSetMaster に失敗しました rc=%d errno=%d', rc, C.get_errno())
            self._close_fd()
            return False

        if not self._set_dpms(DPMS_OFF):
            self._lib.drmDropMaster(self._fd)
            self._close_fd()
            return False
        return True

    def on(self) -> bool:
        """ DPMS=On にして master を手放す。DPMS の成否を返す（master は成否によらず返す） """
        ok = self._set_dpms(DPMS_ON)
        if self._fd is not None:
            self._lib.drmDropMaster(self._fd)
            self._close_fd()
        return ok

    def cleanup(self) -> None:
        """ 消灯中に終了しても画面を点けてから master を手放す """
        if self._fd is not None:
            if self.available:
                self._set_dpms(DPMS_ON)
                self._lib.drmDropMaster(self._fd)
            self._close_fd()
            logger.info('ディスプレイ制御を解放しました')

    def _set_dpms(self, value: int) -> bool:
        """ DPMS プロパティを設定する。connector と prop id は毎回列挙する（固定値にしない） """
        found = self._find_dpms()
        if found is None:
            logger.error('DPMS プロパティを持つコネクタが見つかりませんでした')
            return False

        connector_id, prop_id = found
        rc = self._lib.drmModeConnectorSetProperty(self._fd, connector_id, prop_id, value)
        if rc != 0:
            logger.error('DPMS の設定に失敗しました value=%d rc=%d errno=%d',
                         value, rc, C.get_errno())
            return False
        return True

    def _find_dpms(self) -> tuple[int, int] | None:
        """ 接続済みコネクタの (connector_id, DPMS プロパティ id) を探す """
        res = self._lib.drmModeGetResources(self._fd)
        if not res:
            return None

        try:
            r = res.contents
            for i in range(r.count_connectors):
                connector_id = r.connectors[i]
                cp = self._lib.drmModeGetConnector(self._fd, connector_id)
                if not cp:
                    continue
                try:
                    conn = cp.contents
                    if conn.connection != DRM_MODE_CONNECTED:
                        continue
                    for j in range(conn.count_props):
                        pp = self._lib.drmModeGetProperty(self._fd, conn.props[j])
                        if not pp:
                            continue
                        try:
                            name = pp.contents.name.split(b'\0')[0].decode()
                            if name == 'DPMS':
                                return connector_id, pp.contents.prop_id
                        finally:
                            self._lib.drmModeFreeProperty(pp)
                finally:
                    self._lib.drmModeFreeConnector(cp)
            return None
        finally:
            # 常駐プロセスなので解放漏れを積み上げない
            self._lib.drmModeFreeResources(res)

    def _close_fd(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError as e:
            logger.warning('DRM の fd を閉じられませんでした: %s', e)
        self._fd = None


def _gpio_errors() -> tuple[type[BaseException], ...]:
    """
    GPIO 操作で握る例外の組を返す。

    gpiod 2.5.0 の `RequestReleasedError` は Exception の直接のサブクラスで
    OSError / RuntimeError では捕まらない。gpiod を import できない環境
    （開発環境）でも NameError にならないよう、呼び出し時に解決する。
    """
    errors: tuple[type[BaseException], ...] = (OSError, RuntimeError, ValueError)
    try:
        import gpiod.exception
        errors += (gpiod.exception.RequestReleasedError,)
    except (ImportError, AttributeError):
        pass
    return errors


class _GpioBacklightBackend:
    """
    GPIO でバックライトを叩いて消灯するバックエンド（1号機: HDMI パネル）。

    バックライトを直接切る方が DPMS より復帰の反応が速いため、GPIO で直接制御する。
    DRM master は触らない（SDL の破棄・再生成の流れだけ DPMS 方式と揃えてある）。

    **GPIO は初回に確保して消灯中も保持し続ける。** 消灯のたびに release すると、
    その間ピンの出力が保証されず（入力に戻りフローティングする）、バックライトが
    意図せず点灯・消灯しうるため。確保時の初期値は ACTIVE（点灯）にして、
    確保の瞬間に画面が消えないようにする。

    終了時（`cleanup()`）は High にしてから解放する。解放後も出力値が保たれることは
    1号機のホストで確認済み（2026-11-14、`gpioset` の release 後に `pinctrl get 26` が
    `op lo` / `op hi` のまま。アプリ経由ではない）。
    """

    # 復帰は SDL を再生成した後にバックライトを点ける。先に点けると、
    # SDL が最初のフレームを出すまでの間にコンソールが一瞬見えてしまうため
    restore_after_recreate = True

    def __init__(self, pin: int, chip_path: str) -> None:
        self._pin = pin
        self._chip_path = chip_path
        self._request: Any = None
        self._active: Any = None
        self._inactive: Any = None

        if not IS_RASPBERRY_PI:
            return

        try:
            import gpiod
            from gpiod.line import Direction, Value
            self._active, self._inactive = Value.ACTIVE, Value.INACTIVE
            self._request = gpiod.request_lines(
                self._chip_path,
                consumer=GPIO_CONSUMER_NAME,
                config={self._pin: gpiod.LineSettings(
                    direction=Direction.OUTPUT,
                    output_value=Value.ACTIVE,
                )},
            )
        except (ImportError, OSError, ValueError) as e:
            # GID 不足やデバイス未パススルーでもアプリは動かす（消灯しないだけ）
            logger.warning('バックライトの GPIO を確保できないため画面の消灯は無効になります'
                           '（chip=%s pin=%d）: %s', self._chip_path, self._pin, e)
            self._request = None
            return
        logger.info('バックライトの GPIO を確保しました（chip=%s pin=%d）',
                    self._chip_path, self._pin)

    @property
    def available(self) -> bool:
        """ 実機で GPIO を確保できているか。開発環境では常に False """
        return IS_RASPBERRY_PI and self._request is not None

    def off(self) -> bool:
        """ バックライトを Low にする """
        return self._set(self._inactive)

    def on(self) -> bool:
        """ バックライトを High にする """
        return self._set(self._active)

    def cleanup(self) -> None:
        """
        終了時に画面が消えたまま残らないよう High にしてから GPIO を解放する。

        解放後も出力値が保たれることは1号機のホストで確認済み（2026-11-14。アプリ経由ではない）。
        """
        if self._request is None:
            return
        self._set(self._active)
        try:
            self._request.release()
        except _gpio_errors() as e:
            logger.warning('バックライトの GPIO を解放できませんでした: %s', e)
        self._request = None
        logger.info('ディスプレイ制御を解放しました')

    def _set(self, value: Any) -> bool:
        """ ピンの値を設定する。失敗は例外ではなく False（メインループへ漏らさない） """
        if self._request is None:
            return False
        try:
            self._request.set_value(self._pin, value)
        except _gpio_errors() as e:
            logger.error('バックライトの GPIO を設定できませんでした（pin=%d）: %s', self._pin, e)
            return False
        return True


class DisplayManager:
    """
    ディスプレイの電源を制御するクラス。方式は2つ持ち、コンストラクタで選ぶ。

    -   `drm_dpms`（既定。2号機: HDMI + Full KMS）: DRM DPMS で消灯する。
        `vcgencmd display_power` / `fb0/blank` / CEC はいずれも実運用条件で使えない
        （`.claude/context/known-issues.md` 参照）。

            消灯: on_release_display() -> drmSetMaster() -> DPMS=Off
            復帰: DPMS=On -> drmDropMaster() -> on_recreate_display()

    -   `gpio_backlight`（1号機: Pi 3 Model B + HDMI パネル）: photo-frame と同じく
        バックライトを GPIO（既定 26。High=点灯）で叩く。

            消灯: on_release_display() -> バックライト Low
            復帰: on_recreate_display() -> バックライト High

        復帰で SDL の再生成を先にするのは、先に点けると SDL が最初のフレームを
        出すまでの間にコンソールが一瞬見えるため。

    **SDL の破棄・再生成は方式によらず同じ流れで行う**（呼び出し側の消灯まわりと
    `Renderer.generation` への追従を方式ごとに分岐させないため）。
    共通の状態（点灯状態・復帰の締切）とコールバックの呼び出しはこのクラスが持ち、
    方式ごとの操作は内部のバックエンドへ任せる。
    """

    def __init__(self, on_release_display: Callable[[], None] | None = None,
                 on_recreate_display: Callable[[], None] | None = None,
                 card_path: str = DEFAULT_CARD_PATH,
                 wakeup_delay: float = DEFAULT_WAKEUP_DELAY,
                 method: str = METHOD_DRM_DPMS,
                 backlight_pin: int = DEFAULT_BACKLIGHT_PIN,
                 gpio_chip_path: str = DEFAULT_GPIO_CHIP_PATH) -> None:
        self._on_release = on_release_display
        self._on_recreate = on_recreate_display

        # 設定変更をそのまま反映できるよう属性として公開する
        self.wakeup_delay = wakeup_delay

        if method == METHOD_GPIO_BACKLIGHT:
            self._backend: _DpmsBackend | _GpioBacklightBackend = _GpioBacklightBackend(
                backlight_pin, gpio_chip_path)
        else:
            if method != METHOD_DRM_DPMS:
                logger.warning('未知の消灯方式 %r のため %s を使います', method, METHOD_DRM_DPMS)
            self._backend = _DpmsBackend(card_path)
        self._is_on = True
        # 起動時は既に映っているのでウォームアップは要らない
        self._ready_at = 0.0

    @property
    def is_on(self) -> bool:
        """ 画面が点灯しているとみなしているか """
        return self._is_on

    @property
    def available(self) -> bool:
        """ 実際に消灯できるか。開発環境では常に False """
        return self._backend.available

    @property
    def is_ready(self) -> bool:
        """
        点灯していて、かつ実際に見える状態になったか。

        点灯を指示してもパネルが映るまで数秒かかる。その間に
        省電力タイマーを再開したりスライドを送ると、見えない時間が消費されてしまう。
        **復帰後の処理再開はこのプロパティで判断する。**
        """
        return self._is_on and time.monotonic() >= self._ready_at

    def time_until_ready(self) -> float:
        """
        表示可能になるまでの残り秒数。

        消灯中は 0.0 を返す（復帰指示を出していないため待ち時間が定まらない）。
        判定には is_ready を使い、本メソッドは残り時間の表示にのみ使う。
        """
        if not self._is_on:
            return 0.0
        return max(0.0, self._ready_at - time.monotonic())

    def _begin_wakeup(self) -> None:
        """ 復帰時にウォームアップの締切を設定する """
        self._ready_at = time.monotonic() + max(0.0, self.wakeup_delay)

    def turn_off(self) -> bool:
        """
        画面を消灯する。

        消灯できなくてもアプリは動かし続けたいので、失敗時は例外ではなく False を返す。
        途中で失敗した場合は点灯状態へ巻き戻す。
        """
        if not self._is_on:
            return True

        if not self.available:
            logger.info('画面を消灯します（%s）', _NO_CONTROL_REASON)
            self._call(self._on_release, '描画の破棄')
            self._is_on = False
            return False

        self._call(self._on_release, '描画の破棄')

        if not self._backend.off():
            self._call(self._on_recreate, '描画の再生成')
            return False

        self._is_on = False
        logger.info('画面を消灯しました')
        return True

    def turn_on(self) -> bool:
        """ 画面を復帰させ、描画を再生成する """
        if self._is_on:
            return True

        if not self.available:
            logger.info('画面を復帰します（%s）', _NO_CONTROL_REASON)
            self._is_on = True
            self._begin_wakeup()
            self._call(self._on_recreate, '描画の再生成')
            return False

        if self._backend.restore_after_recreate:
            self._is_on = True
            self._begin_wakeup()
            self._call(self._on_recreate, '描画の再生成')
            ok = self._backend.on()
        else:
            ok = self._backend.on()
            self._is_on = True
            self._begin_wakeup()
            self._call(self._on_recreate, '描画の再生成')
        # 点灯に失敗していても描画は必ず戻す。真っ黒のまま操作不能になるのを避ける
        if ok:
            logger.info('画面を復帰しました（%.1f 秒後に表示可能になる見込み）', self.wakeup_delay)
        return ok

    def cleanup(self) -> None:
        """
        終了時の後片付け。消灯中に終了しても画面を点けてから手放す。

        描画の再生成は行わない（終了処理で SDL を作り直しても無駄なため）。
        """
        self._backend.cleanup()
        self._is_on = True
        self._ready_at = 0.0

    def _call(self, callback: Callable[[], None] | None, label: str) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:
            # コールバックの失敗で電源制御の状態機械を壊さない。握らずログには必ず残す
            logger.exception('%s のコールバックで例外が発生しました', label)
