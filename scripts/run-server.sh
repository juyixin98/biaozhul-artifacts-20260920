#!/usr/bin/env bash
# 启动 HTTP 服务。参数透传，例如：
#   ./scripts/run-server.sh --metric COSINE --port 9090
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d build/classes ]; then
  "$(dirname "$0")/build.sh" >/dev/null
fi

if [ -n "${JAVA_HOME:-}" ]; then JAVA="$JAVA_HOME/bin/java"; else JAVA="${JAVA:-java}"; fi

exec "$JAVA" -cp build/classes vecsearch.Main "$@"
