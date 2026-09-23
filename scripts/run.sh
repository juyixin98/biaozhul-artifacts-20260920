#!/usr/bin/env bash
# Starts the HTTP service. Usage: scripts/run.sh [port]  (default: 8080)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d out ]; then
  ./scripts/build.sh
fi

exec java -cp out joinplanner.Main "${1:-8080}"
