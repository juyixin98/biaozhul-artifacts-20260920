#!/usr/bin/env bash
# 启动 HTTP 服务。用法: scripts/run-server.sh [port]  默认 8080
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="java"
fi

scripts/build.sh
exec $JAVA -cp build/classes intervaljoin.Main server "${1:-8080}"
