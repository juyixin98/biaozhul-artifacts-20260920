#!/usr/bin/env bash
# Start the HTTP service.
# Env: JOIN_PORT (default 8080), JOIN_CONCURRENCY (default 2),
#      JOIN_DATA_DIR (default ./data), JAVA_HOME (optional)
set -euo pipefail
cd "$(dirname "$0")"

JAVA_HOME="${JAVA_HOME:-}"
if [ -n "$JAVA_HOME" ]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="java"
fi

if [ ! -d target/classes ]; then
  ./build.sh
fi

JOIN_PORT="${JOIN_PORT:-8080}"
JOIN_CONCURRENCY="${JOIN_CONCURRENCY:-2}"
JOIN_DATA_DIR="${JOIN_DATA_DIR:-./data}"
export JOIN_PORT JOIN_CONCURRENCY JOIN_DATA_DIR

# Inputs must reside inside the data directory; seed it with the bundled examples.
mkdir -p "$JOIN_DATA_DIR"
cp -f examples/*.jsonl "$JOIN_DATA_DIR"/ 2>/dev/null || true

exec "$JAVA" -cp target/classes join.ApiServer
