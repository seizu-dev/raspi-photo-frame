"""
display_manager.py の実機検証（階層3 / コンテナ）

消灯と復帰は**必ず目視で確認する**。sysfs の dpms が Off を返しても、
それは「映っていない」ことの証明にはなっても「正しく動いた」ことの証明にはならない。

fps が垂直同期（49.61Hz）付近に収まることが、実際に表示されている客観的な根拠になる。
**Renderer に vsync=True を指定し忘れると fps が 1000 超になり、
描画されていないように見える**（実際に一度誤判定しかけた）。

実行方法（実機で）:

    # display_manager.py と本ファイルを ~/dmverify/ へ置いてから
    docker run --rm --device /dev/dri \
      --group-add 44 --group-add 992 --user 1000:1000 \
      -v /run/udev:/run/udev:ro -v "$HOME"/dmverify:/verify:ro \
      -e SDL_VIDEODRIVER=kmsdrm -e SDL_RENDER_DRIVER=opengles2 \
      pi-photo-frame:latest python /verify/verify.py
"""
import logging, os, sys, time
sys.path.insert(0, '/verify')
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s', stream=sys.stdout)

os.environ.setdefault('SDL_VIDEODRIVER', 'kmsdrm')
os.environ['SDL_RENDER_DRIVER'] = 'opengles2'

import pygame as pg
import pygame._sdl2.video as sdl2
from display_manager import DisplayManager

state = {'win': None, 'ren': None}
SYSFS = '/sys/class/drm/card0-HDMI-A-1/dpms'


def sysfs_dpms():
    try:
        return open(SYSFS).read().strip()
    except Exception as e:
        return f'?({e})'


def say(msg):
    print(msg, flush=True)


def create_display():
    pg.display.init()
    info = pg.display.Info()
    w, h = info.current_w, info.current_h
    win = sdl2.Window('pf', size=(w, h), fullscreen_desktop=True)
    state['win'] = win
    # vsync=True でページフリップに同期させる。fps が 49.6 付近に収まることが
    # 「実際に表示されている」根拠になる（main.py と同じ指定）。
    state['ren'] = sdl2.Renderer(win, vsync=True)
    say(f'    [SDL] 再生成 {w}x{h} driver={pg.display.get_driver()}')


def destroy_display():
    state['ren'] = None
    state['win'] = None
    pg.display.quit()
    say('    [SDL] 破棄（DRM master を解放）')


def paint(seconds, color, label):
    ren = state['ren']
    end = time.time() + seconds
    frames = 0
    while time.time() < end:
        ren.draw_color = (*color, 255)
        ren.clear()
        ren.present()
        frames += 1
    fps = frames / seconds
    say(f'    [描画] {label} {seconds}秒 / {frames} frames / {fps:.1f} fps'
        f'{"  ← 垂直同期(49.6)付近" if 40 < fps < 60 else "  ← 要注意: 同期していない"}')


say('=== 実機検証: DRM DPMS による消灯と復帰 ===')
say(f'  開始時の sysfs dpms = {sysfs_dpms()}')

dm = DisplayManager(on_release_display=destroy_display, on_recreate_display=create_display)
say(f'  available = {dm.available} / is_on = {dm.is_on}')

say('')
say('--- 1. 初期描画（赤） 6秒 ---')
create_display()
paint(6, (220, 40, 40), '赤')

say('')
say('--- 2. 消灯 12秒 ★画面が消えることを確認してください ---')
ok = dm.turn_off()
say(f'    turn_off() = {ok} / is_on = {dm.is_on} / sysfs dpms = {sysfs_dpms()}')
time.sleep(12)
say(f'    12秒経過 / sysfs dpms = {sysfs_dpms()}')

say('')
say('--- 3. 復帰して描画（青） 6秒 ★画面が戻り青くなることを確認してください ---')
ok = dm.turn_on()
say(f'    turn_on() = {ok} / is_on = {dm.is_on} / sysfs dpms = {sysfs_dpms()}')
paint(6, (40, 80, 220), '青')

say('')
say('--- 4. 2回目の消灯 8秒 ★もう一度消えることを確認してください ---')
dm.turn_off()
say(f'    sysfs dpms = {sysfs_dpms()}')
time.sleep(8)

say('')
say('--- 5. 2回目の復帰（緑） 6秒 ★戻って緑になることを確認してください ---')
dm.turn_on()
say(f'    sysfs dpms = {sysfs_dpms()}')
paint(6, (40, 200, 80), '緑')

say('')
say('--- 6. 消灯中に cleanup しても画面が戻るか ---')
dm.turn_off()
say(f'    消灯 / sysfs dpms = {sysfs_dpms()}')
time.sleep(5)
dm.cleanup()
say(f'    cleanup() 後 / sysfs dpms = {sysfs_dpms()} / is_on = {dm.is_on}')
say('    ★ 画面が真っ暗のままではなく、点灯状態に戻っていることを確認してください')
time.sleep(3)

destroy_display()
say('')
say('@@@@ ALLDONE @@@@')
