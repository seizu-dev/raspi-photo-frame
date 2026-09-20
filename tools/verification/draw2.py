import os, time
os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
import pygame as pg
pg.display.init()
info = pg.display.Info(); w,h = info.current_w, info.current_h
import pygame._sdl2.video as v

print("=== 利用可能なレンダードライバ ===", flush=True)
try:
    for i, d in enumerate(v.get_drivers()):
        print("  [%d] %s" % (i, d), flush=True)
except Exception as e:
    print("  get_drivers err: %r" % e, flush=True)

win = v.Window("d", size=(w,h), fullscreen_desktop=True)
ren = v.Renderer(win)
pg.mouse.set_visible(False)
print("=== 使用中のレンダラ ===", flush=True)
for attr in ("draw_blend_mode",):
    try: print("  %s = %s" % (attr, getattr(ren, attr)), flush=True)
    except Exception as e: print("  %s err: %r" % (attr, e), flush=True)

def solid(c):
    s = pg.Surface((32,32)); s.fill(c)
    t = v.Texture.from_surface(ren, s)
    print("  Texture %s -> size=%s alpha=%s blend=%s" % (c, t.get_rect().size, t.alpha, t.blend_mode), flush=True)
    return t

print("=== テクスチャ生成 ===", flush=True)
tex_red = solid((230,0,0))
tex_yel = solid((240,230,0))

PH = [
 ("A clear()          -> 全面 濃紺", "clear"),
 ("B Texture.draw()   -> 全面 赤",   "tex_full"),
 ("C fill_rect(全画面)-> 全面 緑",   "fill_full"),
 ("D Texture dstrect  -> 左半分 黄", "tex_half"),
]
SEC=3; CYCLES=10
t0=time.time(); last=-1; total=SEC*len(PH)*CYCLES
while time.time()-t0 < total:
    el=time.time()-t0
    i=int(el/SEC)%len(PH); name,mode=PH[i]
    ren.draw_color=(0,0,120,255); ren.clear()
    if mode=="tex_full":
        tex_red.draw()
    elif mode=="fill_full":
        ren.draw_color=(0,200,0,255); ren.fill_rect(pg.Rect(0,0,w,h))
    elif mode=="tex_half":
        tex_yel.draw(dstrect=pg.Rect(0,0,w//2,h))
    ren.present()
    if i!=last:
        print("t=%3ds %s" % (int(el), name), flush=True); last=i
    pg.time.wait(16)
print("@@@@ ALLDONE @@@@", flush=True)
