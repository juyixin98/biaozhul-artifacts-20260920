#!/usr/bin/env bash
# 启动 HTTP 服务：./run.sh [port]，默认 8080。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build/classes ]; then
    ./build.sh
fi

PORT="${1:-${TVL_PORT:-8080}}"
echo "[run] TVL query executor on port $PORT"
TVL_PORT="$PORT" exec java -cp build/classes com.tvl.Main
