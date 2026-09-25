#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${1:-8080}"
bash scripts/build.sh
exec java -cp build/main com.example.uninorm.Main server "$PORT" data/corpus.txt
