#!/usr/bin/env bash
# Start the JSON-over-HTTP service. Usage: scripts/run.sh [port]
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/build.sh
exec java -cp out/classes com.example.watermark.service.WatermarkServiceMain "${1:-8080}"
