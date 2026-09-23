#!/usr/bin/env bash
# 启动服务：scripts/run.sh [port] [ttlSeconds]
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d build/classes ]; then
  scripts/build.sh
fi

PORT="${1:-${PORT:-8080}}"
SNAPSHOT_TTL="${2:-${SNAPSHOT_TTL:-300}}"

if [ -n "${JAVA_HOME:-}" ]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="$(command -v java)"
fi

PORT="$PORT" SNAPSHOT_TTL="$SNAPSHOT_TTL" \
  "$JAVA" -cp build/classes com.example.paginate.Main
