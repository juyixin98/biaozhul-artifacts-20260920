#!/usr/bin/env bash
# Runs one JSON request through the batch processor and prints the record stream.
#   ./run.sh samples/bridge.json
set -euo pipefail
cd "$(dirname "$0")"

REQUEST="${1:-samples/bridge.json}"
java -cp build/classes com.example.sessionwindow.Main run --file "$REQUEST"
