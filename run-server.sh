#!/usr/bin/env bash
# Build (if needed) and start the JSON HTTP server. Usage: ./run-server.sh [port]
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d out/classes ]; then
  ./build.sh
fi

exec java -cp out/classes com.example.topk.server.Main "${1:-8080}"
