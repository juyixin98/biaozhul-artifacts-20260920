#!/usr/bin/env bash
# First-time local run: create a venv (when possible), install deps, seed
# sample data and start the API on 127.0.0.1:8000.
set -euo pipefail
cd "$(dirname "$0")/.."

export ATMS_DB_PATH="${ATMS_DB_PATH:-data/atms.db}"
export ATMS_HOST="${ATMS_HOST:-127.0.0.1}"
export ATMS_PORT="${ATMS_PORT:-8000}"

python3 -m pip install --user -r requirements.txt
python3 -m scripts.seed_demo "$ATMS_DB_PATH"
exec python3 -m uvicorn atms_backend.api.app:app \
  --host "$ATMS_HOST" --port "$ATMS_PORT"
