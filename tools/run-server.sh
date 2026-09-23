#!/usr/bin/env bash
# Start the HTTP service. Usage: tools/run-server.sh [port]   (default 8080)
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -n "${JAVA_HOME:-}" ]]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="java"
fi

tools/build.sh
exec "$JAVA" -cp classes joinplanner.Main "${1:-8080}"
