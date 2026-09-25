#!/usr/bin/env bash
# Start the JSON/HTTP service (default port 8080, override with $1).
set -euo pipefail
cd "$(dirname "$0")"
./build.sh
java -cp build/classes approxheavy.Main "${1:-8080}"
