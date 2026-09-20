import ctypes as C
import logging
import os
import time
from typing import Callable

logger = logging.getLogger(__name__)

# 開発環境（Dev Container）は x11 で DRM を扱えないため、状態遷移だけを模倣する
IS_RASPBERRY_PI = os.environ.get('IS_DEV_ENVIRONMENT', 'false').lower() != 'true'

DEFAULT_CARD_PATH = '/dev/dri/card0'

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


class DisplayManager:
    """
    ディスプレイの電源を DRM DPMS で制御するクラス

    photo-frame は DSI パネルのバックライトを GPIO 26 で叩いていたが、
    HDMI + Full KMS ではまったく通用しないため新規実装した。
    `vcgencmd display_power` / `fb0/blank` / CEC はいずれも実運用条件で使えない
    （`.claude/context/known-issues.md` 参照）。

    **DRM master は1プロセスしか保持できない。** SDL が master を握ったまま
    別 fd から drmSetMaster すると EACCES で拒否される。そのため消灯時は
    SDL を破棄して master を解放する必要があり、その順序をこのクラスが保証する。

        消灯: on_release_display() -> drmSetMaster() -> DPMS=Off
        復帰: DPMS=On -> drmDropMaster() -> on_recreate_display()

    消灯中は fd と master を保持し続ける（他プロセスに画面を触らせないため）。
    """

    def __init__(self, on_release_display: Callable[[], None] | None = None,
                 on_recreate_display: Callable[[], None] | None = None,
                 card_path: str = DEFAULT_CARD_PATH,
                 wakeup_delay: float = DEFAULT_WAKEUP_DELAY) -> None:
        self._on_release = on_release_display
        self._on_recreate = on_recreate_display
        self._card_path = card_path

        # 設定変更をそのまま反映できるよう属性として公開する
        self.wakeup_delay = wakeup_delay

        self._lib = _load_libdrm() if IS_RASPBERRY_PI else None
        self._fd: int | None = None
        self._is_on = True
        # 起動時は既に映っているのでウォームアップは要らない
        self._ready_at = 0.0

        if IS_RASPBERRY_PI and self._lib is None:
            logger.warning('libdrm を使えないため画面の消灯は無効になります')

    @property
    def is_on(self) -> bool:
        """ 画面が点灯しているとみなしているか """
        return self._is_on

    @property
    def available(self) -> bool:
        """ 実際に DRM で消灯できるか。開発環境では常に False """
        return IS_RASPBERRY_PI and self._lib is not None

    @property
    def is_ready(self) -> bool:
        """
        点灯していて、かつ実際に見える状態になったか。

        DPMS=On を送ってもパネルが映るまで数秒かかる。その間に
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
            logger.info('画面を消灯します（開発環境のため DPMS は操作しません）')
            self._call(self._on_release, '描画の破棄')
            self._is_on = False
            return False

        self._call(self._on_release, '描画の破棄')

        try:
            self._fd = os.open(self._card_path, os.O_RDWR)
        except OSError as e:
            logger.error('DRM デバイスを開けませんでした（%s）: %s', self._card_path, e)
            self._call(self._on_recreate, '描画の再生成')
            return False

        rc = self._lib.drmSetMaster(self._fd)
        if rc != 0:
            # SDL の破棄が済んでいれば通るはず。EACCES ならまだ誰かが master を握っている
            logger.error('drmSetMaster に失敗しました rc=%d errno=%d', rc, C.get_errno())
            self._close_fd()
            self._call(self._on_recreate, '描画の再生成')
            return False

        if not self._set_dpms(DPMS_OFF):
            self._lib.drmDropMaster(self._fd)
            self._close_fd()
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
            logger.info('画面を復帰します（開発環境のため DPMS は操作しません）')
            self._is_on = True
            self._begin_wakeup()
            self._call(self._on_recreate, '描画の再生成')
            return False

        ok = self._set_dpms(DPMS_ON)
        if self._fd is not None:
            self._lib.drmDropMaster(self._fd)
            self._close_fd()

        self._is_on = True
        self._begin_wakeup()
        # DPMS の復帰に失敗していても描画は必ず戻す。真っ黒のまま操作不能になるのを避ける
        self._call(self._on_recreate, '描画の再生成')
        if ok:
            logger.info('画面を復帰しました（%.1f 秒後に表示可能になる見込み）', self.wakeup_delay)
        return ok

    def cleanup(self) -> None:
        """
        終了時の後片付け。消灯中に終了しても画面を点けてから master を手放す。

        描画の再生成は行わない（終了処理で SDL を作り直しても無駄なため）。
        """
        if self._fd is not None:
            if self.available:
                self._set_dpms(DPMS_ON)
                self._lib.drmDropMaster(self._fd)
            self._close_fd()
            logger.info('ディスプレイ制御を解放しました')
        self._is_on = True
        self._ready_at = 0.0

    # ------------------------------------------------------------------ 内部処理

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

    def _call(self, callback: Callable[[], None] | None, label: str) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:
            # コールバックの失敗で電源制御の状態機械を壊さない。握らずログには必ず残す
            logger.exception('%s のコールバックで例外が発生しました', label)
