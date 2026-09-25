#!/usr/bin/env bash
# Build and start the HTTP server (default port 8080).
set -euo pipefail
cd "$(dirname "$0")"

./build.sh
java -cp build/main com.timeconv.http.Server "${1:-8080}"
