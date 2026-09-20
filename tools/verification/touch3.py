import os, time, glob
os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
import pygame as pg
pg.display.init()
info = pg.display.Info(); w, h = info.current_w, info.current_h
print("driver=%s size=%dx%d" % (pg.display.get_driver(), w, h), flush=True)
try:
    from pygame._sdl2 import touch as t
    print("SDL2 touch devices = %d" % t.get_num_devices(), flush=True)
except Exception as e:
    print("touch query failed: %r" % (e,), flush=True)

from pygame._sdl2.video import Window, Renderer, Texture
win = Window("t", size=(w,h), fullscreen_desktop=True)
ren = Renderer(win)
pg.mouse.set_visible(False)

def solid(color, size):
    s = pg.Surface(size); s.fill(color)
    return Texture.from_surface(ren, s)

# fill_rect が効かないため、すべてテクスチャで描画する
tex_corner = solid((200,200,255), (80,80))
tex_bar    = solid((255,255,0),   (8,8))
tex_finger = solid((255,255,255), (36,36))
tex_mouse  = solid((255,150,0),   (36,36))

marks = []
counts = {"FINGERDOWN":0,"FINGERMOTION":0,"FINGERUP":0,
          "MOUSEBUTTONDOWN":0,"MOUSEMOTION":0,"MOUSEBUTTONUP":0}
DUR = 180; t0 = time.time(); last = -1

def redraw():
    el = time.time() - t0
    bg = [(20,20,70),(70,20,20),(20,70,20)][int(el/5) % 3]
    ren.draw_color = (bg[0],bg[1],bg[2],255); ren.clear()
    for rx, ry in ((0,0),(w-80,0),(0,h-80),(w-80,h-80)):
        tex_corner.draw(dstrect=pg.Rect(rx,ry,80,80))
    tex_bar.draw(dstrect=pg.Rect(0, h//2-6, max(1,int(w*(1-el/DUR))), 12))
    for (x,y,kind) in marks[-60:]:
        tex = tex_finger if kind=="f" else tex_mouse
        tex.draw(dstrect=pg.Rect(int(x)-18,int(y)-18,36,36))
    ren.present()

while time.time() - t0 < DUR:
    for e in pg.event.get():
        nm = pg.event.event_name(e.type).upper()
        if nm in counts: counts[nm] += 1
        if nm in ("FINGERDOWN","FINGERMOTION"):
            marks.append((e.x*w, e.y*h, "f"))
            print("%-14s norm=(%.3f,%.3f) px=(%d,%d)" % (nm,e.x,e.y,e.x*w,e.y*h), flush=True)
        elif nm in ("MOUSEBUTTONDOWN","MOUSEMOTION"):
            marks.append((e.pos[0], e.pos[1], "m"))
            print("%-14s pos=(%d,%d)" % (nm,e.pos[0],e.pos[1]), flush=True)
        elif nm in ("FINGERUP","MOUSEBUTTONUP"):
            print("%-14s" % nm, flush=True)
    redraw()
    el = int(time.time()-t0)
    if el != last and el % 20 == 0:
        print("--- t=%3ds counts=%s" % (el,counts), flush=True); last = el
    pg.time.wait(16)

print("FINAL = %s" % counts, flush=True)
real = counts["FINGERDOWN"]+counts["FINGERUP"]+counts["MOUSEBUTTONDOWN"]+counts["MOUSEBUTTONUP"]
print("RESULT: %s" % ("INPUT_DETECTED" if real>0 else "NO_INPUT"), flush=True)
