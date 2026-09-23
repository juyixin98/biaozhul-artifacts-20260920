#!/usr/bin/env bash
# 启动服务。用法: ./scripts/run.sh [port] [windowSize] [allowedLateness]
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="$(command -v java)"
fi

if [ ! -d out/tumbling ]; then
  ./scripts/compile.sh
fi

exec "$JAVA" -cp out tumbling.Main "$@"
