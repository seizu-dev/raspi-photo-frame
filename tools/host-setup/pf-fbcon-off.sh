#!/bin/sh
# フレームバッファコンソールを切り離し、フレームバッファをゼロクリアする。
#
# 消灯時に SDL を破棄すると SDL 自身のフレームバッファも解放され、
# CRTC は /dev/fb0（fbcon 用 / 1024x600 16bpp）を指し直してスキャンアウトする。
# そこにブート時のコンソール出力が残っていると、消灯・復帰のたびに文字が一瞬見える。
#
# unbind だけでは「以降書かれない」だけで既存の内容は消えないため、
# ゼロクリアまで行う。SDL が DRM master を握っている間は fb0 への書き込みが
# 効かないので、アプリより先に実行する必要がある（unit の Before=docker.service）。

for d in /sys/class/vtconsole/vtcon*; do
    if grep -q 'frame buffer device' "$d/name" 2>/dev/null; then
        echo 0 > "$d/bind" 2>/dev/null || true
    fi
done

if [ -e /dev/fb0 ]; then
    # 容量を超えると ENOSPC で止まるが、全面を書き終えているので無視してよい
    dd if=/dev/zero of=/dev/fb0 bs=1M 2>/dev/null || true
fi

exit 0
