import os, time
os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
os.environ["SDL_RENDER_DRIVER"] = "opengles2"
import pygame as pg
import pygame._sdl2.video as v
pg.display.init()
info = pg.display.Info(); w,h = info.current_w, info.current_h
win = v.Window("v", size=(w,h), fullscreen_desktop=True)
ren = v.Renderer(win)
pg.mouse.set_visible(False)
print("size=%dx%d driver=%s" % (w,h,pg.display.get_driver()), flush=True)

def tex(c, size=(64,64)):
    s = pg.Surface(size); s.fill(c); return v.Texture.from_surface(ren, s)

t_red=tex((220,40,40)); t_blu=tex((40,80,220)); t_wht=tex((255,255,255)); t_ylw=tex((250,230,40))

# --- Phase 1: 全画面テクスチャ 12秒 ---
print("PHASE1 全画面テクスチャ (赤/青 交互) %s" % time.strftime("%T"), flush=True)
t0=time.time()
while time.time()-t0 < 12:
    ren.draw_color=(0,0,0,255); ren.clear()
    (t_red if int((time.time()-t0)/2)%2==0 else t_blu).draw()
    ren.present(); pg.time.wait(16)

# --- Phase 2: 部分描画 12秒 ---
print("PHASE2 部分描画 (四隅の白 + 動く黄バー) %s" % time.strftime("%T"), flush=True)
t0=time.time()
while time.time()-t0 < 12:
    ren.draw_color=(20,20,60,255); ren.clear()
    for rx,ry in ((0,0),(w-100,0),(0,h-100),(w-100,h-100)):
        t_wht.draw(dstrect=pg.Rect(rx,ry,100,100))
    x=int((w-120)*((time.time()-t0)%4)/4)
    t_ylw.draw(dstrect=pg.Rect(x, h//2-30, 120, 60))
    ren.present(); pg.time.wait(16)

# --- Phase 3: クロスフェード fps 実測 10秒 ---
print("PHASE3 クロスフェード計測 %s" % time.strftime("%T"), flush=True)
sa=pg.Surface((w,h)); sa.fill((200,60,60))
sb=pg.Surface((w,h)); sb.fill((60,60,200))
ta=v.Texture.from_surface(ren,sa); tb=v.Texture.from_surface(ren,sb)
tb.blend_mode = pg.BLENDMODE_BLEND
frames=0; t0=time.time(); DUR=10.0
while True:
    el=time.time()-t0
    if el>DUR: break
    ren.draw_color=(0,0,0,255); ren.clear()
    ta.draw()
    tb.alpha=int(255*((el/2.0)%1.0))
    tb.draw()
    ren.present(); frames+=1
el=time.time()-t0
print("CROSSFADE frames=%d elapsed=%.2f fps=%.1f" % (frames, el, frames/el), flush=True)
import resource
print("maxrss_kb=%d" % resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, flush=True)
print("@@@@ ALLDONE @@@@", flush=True)
