#!/usr/bin/env bash
# Start the vector search HTTP server.
# Overridable: PORT=9090 scripts/run-server.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -n "${JAVA_HOME:-}" && -x "$JAVA_HOME/bin/java" ]]; then
  JAVA_BIN="$JAVA_HOME/bin/java"
elif [[ -x "$HOME/tools/jdk-21.0.5+11/bin/java" ]]; then
  JAVA_BIN="$HOME/tools/jdk-21.0.5+11/bin/java"
else
  JAVA_BIN="java"
fi

if [[ ! -d out/classes ]]; then
  scripts/build.sh
fi

PORT="${PORT:-8080}"
exec "$JAVA_BIN" -Dport="$PORT" -cp out/classes com.example.vecsearch.HttpServerMain
