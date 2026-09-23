#!/usr/bin/env bash
# Run the server: ./run.sh [--port 8080] [--data data]
set -euo pipefail
cd "$(dirname "$0")"
if [ -n "${JAVA_HOME:-}" ]; then JAVA="$JAVA_HOME/bin/java"; else JAVA="java"; fi
[ -d out ] || ./build.sh
exec "$JAVA" -cp out colscan.Server "$@"
