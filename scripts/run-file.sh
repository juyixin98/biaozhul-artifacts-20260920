#!/usr/bin/env bash
# Run a request JSON file locally (no HTTP) and print the JSON result.
# Usage: scripts/run-file.sh examples/pause-resume.json
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/build.sh >/dev/null
java -cp out/classes com.example.watermark.service.WatermarkScriptRunner "$1"
