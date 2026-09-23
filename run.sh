#!/usr/bin/env bash
# 启动 HTTP 服务。用法: ./run.sh [port]，默认 8080，也可用 PORT 环境变量。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build/classes ]; then
  ./build.sh
fi
JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"
exec $JAVA_BIN -cp build/classes com.example.intervalindex.http.IntervalServer "${1:-${PORT:-8080}}"
