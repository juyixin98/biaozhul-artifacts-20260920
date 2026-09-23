#!/usr/bin/env bash
# Starts the HTTP service. Configurable via env:
#   PORT=8080 HOST=0.0.0.0 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

JAVA_HOME="${JAVA_HOME:-}"
if [[ -n "$JAVA_HOME" ]]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="java"
fi

if [[ ! -d out/main ]]; then
  ./build.sh
fi

exec "$JAVA" -cp out/main com.example.iview.server.Main
