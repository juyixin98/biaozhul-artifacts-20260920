#!/usr/bin/env bash
# Start the service.
#   ./scripts/run.sh [port]
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"
PORT="${1:-8080}"

if [ ! -d out/classes ]; then
  ./scripts/build.sh
fi

exec "$JAVA_BIN" -cp out/classes com.example.sessionwindow.Main \
  --port "$PORT" --gap 10 --allowed-lateness 10 --data-dir run/data
