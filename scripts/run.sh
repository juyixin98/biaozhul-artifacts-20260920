#!/usr/bin/env bash
# 启动列式分片扫描服务。
# 用法: scripts/run.sh [端口] [数据目录]
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA="${JAVA_HOME:+$JAVA_HOME/bin/}java"
PORT="${1:-${PORT:-8080}}"
DIR="${2:-${DATA_DIR:-./data}}"

if [ ! -d build/classes ]; then
  scripts/build.sh
fi

exec "$JAVA" -Dserver.port="$PORT" -Dserver.host=0.0.0.0 -Ddata.dir="$DIR" \
  colscan.server.Main
