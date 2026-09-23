#!/usr/bin/env bash
# Starts the HTTP server. Usage:
#   ./run.sh [--port 8080] [--window-size 10] [--allowed-lateness 2]
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck source=scripts/resolve-jdk.sh
source scripts/resolve-jdk.sh

if [ ! -d build/classes ]; then
  ./build.sh
fi

exec "$JAVA" -cp build/classes com.example.window.WindowServer "$@"
