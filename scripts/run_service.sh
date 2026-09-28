#!/usr/bin/env bash
# Start the STRIPS planning service with local SQLite + JSONL evidence.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

export STRIPS_DB_PATH="${STRIPS_DB_PATH:-data/evidence.db}"
export STRIPS_LOG_PATH="${STRIPS_LOG_PATH:-logs/runs.jsonl}"
mkdir -p data logs

exec python3 -m uvicorn strips_planner.main:app \
  --host "${STRIPS_HOST:-127.0.0.1}" \
  --port "${STRIPS_PORT:-8000}"
