#!/usr/bin/env bash
# 把 samples/sample_request.bin 通过 TCP 发给运行中的 mpstream-server。
# 用法: scripts/send_sample.sh [addr]   （默认 127.0.0.1:18080）
set -euo pipefail
ADDR="${1:-127.0.0.1:18080}"
HOST="${ADDR%:*}"
PORT="${ADDR##*:}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$HERE/../samples/sample_request.bin" ]; then
    python3 "$HERE/make_sample.py"
fi

# nc 的 -N 参数表示发送完毕后关闭写方向（shutdown），服务端据此判定输入结束。
nc -N "$HOST" "$PORT" < "$HERE/../samples/sample_request.bin"
