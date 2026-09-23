#!/usr/bin/env bash
# 启动 HTTP 服务。用法：scripts/run.sh [port]
set -euo pipefail
cd "$(dirname "$0")/.."
JAVA_BIN="$(bash scripts/find-java.sh)"
if [ ! -d target/classes ]; then
  bash scripts/build.sh
fi
exec "$JAVA_BIN/java" -cp target/classes ij.Main "$@"
