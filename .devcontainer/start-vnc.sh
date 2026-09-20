#!/bin/bash

# 実機ディスプレイと同じ解像度に合わせる（1024x600 @ 49.61Hz。SPECIFICATION.md 9-9）。
# photo-frame（Pi 3 Model B + 公式7インチDSIディスプレイ想定の 1280x800）から移植する際に変更。
export DISPLAY=:1
export RESOLUTION=1024x600x24

echo "Starting Xvfb on $DISPLAY with resolution $RESOLUTION..."
Xvfb $DISPLAY -screen 0 $RESOLUTION &
sleep 2

echo "Starting Fluxbox..."
fluxbox &

echo "Starting x11vnc..."
env -u WAYLAND_DISPLAY x11vnc -clip 1024x600+0+0 -display $DISPLAY -nopw -listen localhost -xkb -forever &

echo "Starting noVNC..."
# Debianのnovncパッケージの標準パスを使用
websockify --web /usr/share/novnc/ 6080 localhost:5900 &

echo "VNC Environment Started."
echo "Access via browser: http://localhost:6080/vnc.html?show_dot=true"
