"""libdrm を ctypes で叩いて DPMS を操作する検証スクリプト"""
import ctypes as C, os, sys, time

lib = C.CDLL("libdrm.so.2", use_errno=True)

class Res(C.Structure):
    _fields_ = [("count_fbs", C.c_int), ("fbs", C.POINTER(C.c_uint32)),
                ("count_crtcs", C.c_int), ("crtcs", C.POINTER(C.c_uint32)),
                ("count_connectors", C.c_int), ("connectors", C.POINTER(C.c_uint32)),
                ("count_encoders", C.c_int), ("encoders", C.POINTER(C.c_uint32)),
                ("min_width", C.c_uint32), ("max_width", C.c_uint32),
                ("min_height", C.c_uint32), ("max_height", C.c_uint32)]

class Conn(C.Structure):
    _fields_ = [("connector_id", C.c_uint32), ("encoder_id", C.c_uint32),
                ("connector_type", C.c_uint32), ("connector_type_id", C.c_uint32),
                ("connection", C.c_int), ("mmWidth", C.c_uint32), ("mmHeight", C.c_uint32),
                ("subpixel", C.c_int), ("count_modes", C.c_int),
                ("modes", C.c_void_p), ("count_props", C.c_int),
                ("props", C.POINTER(C.c_uint32)), ("prop_values", C.POINTER(C.c_uint64)),
                ("count_encoders", C.c_int), ("encoders", C.POINTER(C.c_uint32))]

class Prop(C.Structure):
    _fields_ = [("prop_id", C.c_uint32), ("flags", C.c_uint32), ("name", C.c_char*32),
                ("count_values", C.c_int), ("values", C.POINTER(C.c_uint64)),
                ("count_enums", C.c_int), ("enums", C.c_void_p),
                ("count_blobs", C.c_int), ("blob_ids", C.POINTER(C.c_uint32)),
                ("blob_values", C.POINTER(C.c_uint32))]

lib.drmModeGetResources.restype = C.POINTER(Res)
lib.drmModeGetConnector.restype = C.POINTER(Conn)
lib.drmModeGetProperty.restype = C.POINTER(Prop)
lib.drmModeConnectorSetProperty.restype = C.c_int
lib.drmSetMaster.restype = C.c_int
lib.drmDropMaster.restype = C.c_int

DEV = "/dev/dri/card0"

def find_dpms(fd):
    """(connector_id, dpms_prop_id, 現在値) を返す"""
    res = lib.drmModeGetResources(fd)
    if not res:
        return None, None, None
    r = res.contents
    for i in range(r.count_connectors):
        cid = r.connectors[i]
        cp = lib.drmModeGetConnector(fd, cid)
        if not cp: continue
        c = cp.contents
        if c.connection != 1:   # 1 = connected
            lib.drmModeFreeConnector(cp); continue
        for j in range(c.count_props):
            pp = lib.drmModeGetProperty(fd, c.props[j])
            if not pp: continue
            nm = pp.contents.name.split(b"\0")[0].decode()
            if nm == "DPMS":
                pid = pp.contents.prop_id; val = c.prop_values[j]
                lib.drmModeFreeProperty(pp); lib.drmModeFreeConnector(cp)
                return cid, pid, val
            lib.drmModeFreeProperty(pp)
        lib.drmModeFreeConnector(cp)
    return None, None, None

def set_dpms(fd, cid, pid, value):
    rc = lib.drmModeConnectorSetProperty(fd, cid, pid, value)
    err = C.get_errno()
    return rc, err

def report(tag):
    try:
        s = open("/sys/class/drm/card0-HDMI-A-1/dpms").read().strip()
    except Exception as e:
        s = "?(%s)" % e
    print("      %-12s sysfs dpms=%s" % (tag, s), flush=True)

STATES = {0:"On", 1:"Standby", 2:"Suspend", 3:"Off"}

print("=== PHASE 1: SDL なし、単独で DPMS 操作 ===", flush=True)
fd = os.open(DEV, os.O_RDWR)
print("  open(%s) fd=%d" % (DEV, fd), flush=True)
rc = lib.drmSetMaster(fd); print("  drmSetMaster rc=%d errno=%d" % (rc, C.get_errno()), flush=True)
cid, pid, cur = find_dpms(fd)
print("  connector=%s dpms_prop=%s 現在値=%s(%s)" % (cid, pid, cur, STATES.get(cur)), flush=True)
if cid:
    report("before")
    rc, err = set_dpms(fd, cid, pid, 3)
    print("  set DPMS=Off  rc=%d errno=%d" % (rc, err), flush=True)
    time.sleep(2); report("after-off")
    print("  ★ 12秒間 消灯を確認してください", flush=True)
    time.sleep(12)
    rc, err = set_dpms(fd, cid, pid, 0)
    print("  set DPMS=On   rc=%d errno=%d" % (rc, err), flush=True)
    time.sleep(2); report("after-on")
lib.drmDropMaster(fd); os.close(fd)
time.sleep(3)

print("", flush=True)
print("=== PHASE 2: SDL が master を握った状態で、別 fd から DPMS 操作 ===", flush=True)
os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
os.environ["SDL_RENDER_DRIVER"] = "opengles2"
import pygame as pg, pygame._sdl2.video as v
pg.display.init()
i = pg.display.Info(); w, h = i.current_w, i.current_h
win = v.Window("d", size=(w,h), fullscreen_desktop=True); ren = v.Renderer(win)
s = pg.Surface((32,32)); s.fill((40,200,40)); tex = v.Texture.from_surface(ren, s)
def paint(n=30):
    for _ in range(n):
        ren.draw_color=(0,0,0,255); ren.clear(); tex.draw(); ren.present(); pg.time.wait(16)
paint(60)
print("  SDL 描画中（緑）", flush=True)
fd2 = os.open(DEV, os.O_RDWR)
rc = lib.drmSetMaster(fd2); print("  drmSetMaster rc=%d errno=%d (master 競合の想定)" % (rc, C.get_errno()), flush=True)
cid2, pid2, cur2 = find_dpms(fd2)
if cid2:
    rc, err = set_dpms(fd2, cid2, pid2, 3)
    print("  set DPMS=Off rc=%d errno=%d" % (rc, err), flush=True)
    time.sleep(2); report("phase2")
    print("  ★ 8秒間 消灯するか確認してください", flush=True)
    for _ in range(8): paint(60)
    set_dpms(fd2, cid2, pid2, 0)
os.close(fd2)

print("", flush=True)
print("=== PHASE 3: SDL を破棄 -> DPMS Off -> 復帰 -> SDL 再生成 ===", flush=True)
paint(60)
del ren, win
pg.display.quit()
print("  SDL を破棄（master 解放）", flush=True)
time.sleep(1)
fd3 = os.open(DEV, os.O_RDWR)
rc = lib.drmSetMaster(fd3); print("  drmSetMaster rc=%d errno=%d" % (rc, C.get_errno()), flush=True)
cid3, pid3, _ = find_dpms(fd3)
if cid3:
    rc, err = set_dpms(fd3, cid3, pid3, 3)
    print("  set DPMS=Off rc=%d errno=%d" % (rc, err), flush=True)
    time.sleep(2); report("phase3-off")
    print("  ★ 12秒間 消灯を確認してください", flush=True)
    time.sleep(12)
    rc, err = set_dpms(fd3, cid3, pid3, 0)
    print("  set DPMS=On  rc=%d errno=%d" % (rc, err), flush=True)
    time.sleep(1); report("phase3-on")
lib.drmDropMaster(fd3); os.close(fd3)
print("  SDL 再生成して描画（青）", flush=True)
pg.display.init()
i = pg.display.Info(); w,h = i.current_w, i.current_h
win = v.Window("d2", size=(w,h), fullscreen_desktop=True); ren = v.Renderer(win)
s = pg.Surface((32,32)); s.fill((40,80,220)); tex = v.Texture.from_surface(ren, s)
paint(300)
print("@@@@ ALLDONE @@@@", flush=True)
