#!/usr/bin/env bash
# 启动三值逻辑查询 HTTP 服务。
# 用法：PORT=8080 ./run.sh   （默认端口 8080）
set -euo pipefail
cd "$(dirname "$0")"

JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"
JAVA="${JAVA:-$JAVA_BIN}"
PORT="${PORT:-8080}"

if [ ! -d out/classes ]; then
  ./build.sh
fi
exec "$JAVA" -Dserver.port="$PORT" -cp out/classes com.example.tvl.http.HttpServerMain
