#!/usr/bin/env bash
# Run the HTTP server. All settings have sensible defaults and can be overridden
# by environment variables or -D system properties of the same name.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="${JAVA:-java}"
fi

if [ ! -d build/classes ]; then
  echo ">> build/classes missing, building first..." >&2
  ./scripts/build.sh
fi

export PORT="${PORT:-8080}"
export GAP_MS="${GAP_MS:-10}"
export ALLOWED_LATENESS_MS="${ALLOWED_LATENESS_MS:-10}"
export DATA_DIR="${DATA_DIR:-data}"

echo ">> Starting on port ${PORT} (gap=${GAP_MS}ms, lateness=${ALLOWED_LATENESS_MS}ms, data=${DATA_DIR})"
exec "$JAVA" -cp build/classes sessionwindow.ServerMain
