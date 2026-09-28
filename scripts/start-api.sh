#!/usr/bin/env bash
# Start the local FastAPI service (real uvicorn, no external accounts).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="${PYTHONPATH:-}:${ROOT}/src"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo "Starting adam-shard API on http://${HOST}:${PORT}"
exec python3 -m uvicorn adam_shard.api:app --host "${HOST}" --port "${PORT}"
