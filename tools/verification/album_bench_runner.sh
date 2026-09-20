#!/usr/bin/env bash
#
# 実機（階層3）でアルバムグリッドのベンチを件数ごとに回すランナー
#
# HDMI は1枚しかないため、ベンチ中はアプリを止める必要がある。
# **失敗しても必ずアプリを起動し直す**（trap で戻す）。
#
# 使い方（実機で。Cloudflare Tunnel の切断を避けるため切り離して実行する）:
#
#   setsid nohup bash ~/pi-photo-frame/album_bench_runner.sh \
#       > ~/pi-photo-frame/album_bench.log 2>&1 < /dev/null &
#   grep -q RUNNER_ALLDONE ~/pi-photo-frame/album_bench.log
#
# アプリの配置先が ~/pi-photo-frame でない場合は APP_DIR で上書きする。
#
# 件数は引数で上書きできる（既定 13 50 150 300 600）。
#
# **キャッシュと設定はベンチ専用ディレクトリへ分離する。** AlbumScreen は
# 取得したアルバム一覧で cleanup_thumbnails() を呼ぶため、本番の /cache を
# 渡すと本番のサムネイルが消える。
set -u

APP_DIR="${APP_DIR:-$HOME/pi-photo-frame}"
BENCH_DIR="${APP_DIR}/bench"
COUNTS="${*:-13 50 150 300 600}"

cd "${APP_DIR}" || exit 1
mkdir -p "${BENCH_DIR}/cache" "${BENCH_DIR}/config"

restore_app() {
    echo "--- アプリを起動し直します"
    docker compose start
    echo "RUNNER_ALLDONE"
}
trap restore_app EXIT

echo "--- 測定前の状態"
vcgencmd measure_temp
vcgencmd get_throttled
free -m | head -2

echo "--- アプリを停止します"
docker compose stop

for N in ${COUNTS}; do
    echo "=== count=${N} 開始 $(date '+%F %T')"
    docker run --rm \
        --user 1000:1000 \
        --group-add 44 --group-add 992 --group-add 996 \
        --device /dev/dri --device /dev/input \
        -v /run/udev:/run/udev:ro \
        -v "${APP_DIR}/tools:/app/tools:ro" \
        -v "${BENCH_DIR}/cache:/bench-cache" \
        -v "${BENCH_DIR}/config:/bench-config" \
        -v /etc/localtime:/etc/localtime:ro \
        --env-file "${APP_DIR}/.env" \
        -e SDL_VIDEODRIVER=kmsdrm \
        -e SDL_RENDER_DRIVER=opengles2 \
        -e PYTHONUNBUFFERED=1 \
        pi-photo-frame:latest \
        python tools/verification/album_grid_bench.py \
            --count "${N}" \
            --cache-dir /bench-cache \
            --config-dir /bench-config
    echo "=== count=${N} 終了 exit=$? $(date '+%F %T')"
    free -m | head -2
done

echo "--- 測定後の状態"
vcgencmd measure_temp
vcgencmd get_throttled
du -sh "${BENCH_DIR}/cache"
