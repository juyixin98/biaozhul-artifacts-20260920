#!/usr/bin/env bash
# 启动 JSON 服务。用法: ./run-server.sh [port] [bind-host]
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d build/classes ]; then
  ./build.sh
fi
PORT="${1:-8080}"
HOST="${2:-127.0.0.1}"
java -cp build/classes com.example.ac.server.HttpJsonServer "$PORT" "$HOST"
