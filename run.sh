#!/usr/bin/env bash
# Builds (if needed) and starts the HTTP server.
# Env: PORT (default 8080), PAGER_SNAPSHOT_TTL_MS (default 60000),
#      PAGER_MAC_SECRET (default: random per process — cursors won't survive restarts).
set -euo pipefail
cd "$(dirname "$0")"

if [ -n "${JAVA_HOME:-}" ]; then JAVA="$JAVA_HOME/bin/java"; else JAVA="$(command -v java)"; fi
if [ -z "${JAVA:-}" ] || ! "$JAVA" -version >/dev/null 2>&1; then
  echo "error: java not found. Install JDK 17+ and set JAVA_HOME (see README.md)." >&2
  exit 1
fi

if [ ! -d build ] || [ -z "$(find build -name '*.class' -print -quit 2>/dev/null)" ]; then
  ./build.sh
fi

exec "$JAVA" -cp build com.example.stablepager.Main
