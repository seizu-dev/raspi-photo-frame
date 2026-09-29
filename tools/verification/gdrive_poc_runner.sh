#!/usr/bin/env bash
# 実機用ランナー（PR0 / PoC）: gdrive_probe.py / heic_decode_bench.py に要る依存を
# site ディレクトリへ入れてから、samples/ 配下の画像で heic_decode_bench.py を回す。
#
# .claude/plans/abundant-weaving-kernighan.md PR0 の階層3測定
# （HEIC 12MP のピークメモリと所要時間）に使う。**本番コードには一切触れない。**
#
# 前提:
#   - $WORK_DIR/samples/ に測定対象の画像（JPEG 12MP・HEIC 12MP・HEIC 48MP・PNG 等）を
#     あらかじめ置いておくこと（ユーザー作業。SA 鍵と一緒に tools/ や samples/ に
#     コミットしないこと）
#   - アプリは止めない。稼働中の状態で測る（停止して測りたい場合は、この
#     スクリプトを呼ぶ側で先に `docker compose stop` すること）。CPU を占有する別プロセスにも触らない
#
# 使い方（実機で、切り離して実行する）:
#   setsid nohup bash tools/verification/gdrive_poc_runner.sh \
#       > gdrive_poc_runner.log 2>&1 < /dev/null &
#   grep -q RUNNER_ALLDONE gdrive_poc_runner.log
#
# PILLOW_VERSION は既定で現行の 10.4.0（requirements.txt と同じ）を使う。
# pillow-heif 1.8.0 系は Pillow 11.1 以上を要求するため、既定の 10.4.0 のままでは
# HEIC を扱う decode ケースは pillow-heif の import に失敗する（import-only は影響しない）。
# HEIC のピークメモリを測る場合は PILLOW_VERSION=11.1.0 以上を明示すること。

set -u

WORK_DIR="${WORK_DIR:-$HOME/work/gdpoc}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 既定は requirements.txt と同じ 10.4.0（必須ではなくなった。PR0 の調査で確定した
# 依存追加なし方針＝Pillow 更新も pillow-heif も見送りに合わせた既定値）
PILLOW_VERSION="${PILLOW_VERSION:-10.4.0}"
PILLOW_HEIF_VERSION="${PILLOW_HEIF_VERSION:-1.8.0}"
GOOGLE_AUTH_VERSION="${GOOGLE_AUTH_VERSION:-2.58.1}"
REQUESTS_VERSION="${REQUESTS_VERSION:-2.32.5}"

# pillow-heif 1.8.0 系は Pillow>=11.1 を要求する。Pillow のメジャー.マイナーを
# 比較し、11.1 未満なら pillow-heif は入れない（依存解決の失敗でインストール
# 全体が止まるのを避けるため）。バージョン文字列の比較は sort -V に任せる。
INSTALL_PILLOW_HEIF=1
MIN_PILLOW_FOR_HEIF="11.1"
if [ "$(printf '%s\n%s\n' "$MIN_PILLOW_FOR_HEIF" "$PILLOW_VERSION" | sort -V | head -n1)" != "$MIN_PILLOW_FOR_HEIF" ]; then
    INSTALL_PILLOW_HEIF=0
    echo "PILLOW_VERSION=$PILLOW_VERSION は pillow-heif ${PILLOW_HEIF_VERSION} の要求" \
         "（Pillow>=${MIN_PILLOW_FOR_HEIF}）を満たさないため、pillow-heif は" \
         "インストールしません（HEIC の decode ケースは失敗します。import-only は" \
         "影響を受けません）"
fi

VMSTAT_PID=""
cleanup() {
    if [ -n "$VMSTAT_PID" ] && kill -0 "$VMSTAT_PID" 2>/dev/null; then
        kill "$VMSTAT_PID" 2>/dev/null
    fi
}
trap cleanup EXIT

mkdir -p "$WORK_DIR/site" "$WORK_DIR/samples"

echo "=== 測定開始時点の状態 ==="
free -m
docker ps --format '{{.Names}} {{.Status}}'
echo

echo "=== 依存のインストール（Pillow==${PILLOW_VERSION} / pillow-heif導入=${INSTALL_PILLOW_HEIF}） ==="
PIP_PACKAGES=("Pillow==${PILLOW_VERSION}" "google-auth==${GOOGLE_AUTH_VERSION}" "requests==${REQUESTS_VERSION}")
if [ "$INSTALL_PILLOW_HEIF" -eq 1 ]; then
    PIP_PACKAGES+=("pillow-heif==${PILLOW_HEIF_VERSION}")
fi
docker run --rm -v "$WORK_DIR":/w python:3.13-slim \
    pip install --only-binary=:all: --no-cache-dir --target /w/site \
    "${PIP_PACKAGES[@]}"
INSTALL_EXIT=$?
echo "INSTALL_EXIT=$INSTALL_EXIT"
if [ "$INSTALL_EXIT" -ne 0 ]; then
    echo "依存のインストールに失敗しました。中止します" >&2
    exit 1
fi

echo "=== vmstat の記録を開始 ==="
vmstat 1 > "$WORK_DIR/vmstat.log" 2>&1 &
VMSTAT_PID=$!

SAMPLES=("$WORK_DIR"/samples/*)
if [ ! -e "${SAMPLES[0]}" ]; then
    echo "samples/ に画像がありません: $WORK_DIR/samples" >&2
    exit 1
fi

# pillow-heif を入れていない（Pillow<11.1）ときは decode ケースにも --no-heif を
# 渡す。渡さないと heic_decode_bench.py の pillow_heif import で全サンプルが
# 一律 exit 2 になり、JPEG サンプルの測定までできなくなるため。
# HEIC サンプルはこのフラグでも Image.open() が開けずに失敗するが、それは
# 「pillow-heif が無いと HEIC は測れない」という制約どおりの失敗である。
DECODE_EXTRA_ARGS=()
if [ "$INSTALL_PILLOW_HEIF" -eq 0 ]; then
    DECODE_EXTRA_ARGS+=(--no-heif)
fi

for sample in "${SAMPLES[@]}"; do
    name="$(basename "$sample")"
    echo "=== $name: import-only ==="
    docker run --rm --user 1000:1000 \
        -v "$WORK_DIR":/w -v "$SCRIPT_DIR":/tools:ro \
        -e PYTHONPATH=/w/site \
        python:3.13-slim \
        python /tools/heic_decode_bench.py "/w/samples/$name" --mode import-only

    echo "=== $name: decode（MALLOC_ARENA_MAX 未設定） ==="
    docker run --rm --user 1000:1000 \
        -v "$WORK_DIR":/w -v "$SCRIPT_DIR":/tools:ro \
        -e PYTHONPATH=/w/site \
        python:3.13-slim \
        python /tools/heic_decode_bench.py "/w/samples/$name" --mode decode "${DECODE_EXTRA_ARGS[@]}"

    echo "=== $name: decode（MALLOC_ARENA_MAX=2） ==="
    docker run --rm --user 1000:1000 \
        -v "$WORK_DIR":/w -v "$SCRIPT_DIR":/tools:ro \
        -e PYTHONPATH=/w/site -e MALLOC_ARENA_MAX=2 \
        python:3.13-slim \
        python /tools/heic_decode_bench.py "/w/samples/$name" --mode decode "${DECODE_EXTRA_ARGS[@]}"
done

kill "$VMSTAT_PID" 2>/dev/null
VMSTAT_PID=""

echo "=== 測定終了時点の状態 ==="
free -m
docker ps --format '{{.Names}} {{.Status}}'

echo "RUNNER_ALLDONE"
