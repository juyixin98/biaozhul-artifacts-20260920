#!/usr/bin/env bash
# Start the JSON service on 127.0.0.1:8080 (override port with $1).
set -euo pipefail
cd "$(dirname "$0")"

./build.sh
java -cp build/classes com.example.uninorm.Main "${1:-8080}"
