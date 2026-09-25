#!/usr/bin/env bash
# Start the local JSON HTTP service (default port 8080).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8080}"

bash "$ROOT/scripts/build.sh" >/dev/null
exec java -cp "$ROOT/out/classes" neardup.Main serve "$PORT"
