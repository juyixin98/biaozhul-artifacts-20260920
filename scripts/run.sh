#!/usr/bin/env bash
# Start the sample size planner API locally. No external services required.
set -euo pipefail
cd "$(dirname "$0")/.."

export SSP_DB_PATH="${SSP_DB_PATH:-data/plans.db}"
export SSP_LOG_DIR="${SSP_LOG_DIR:-logs}"
export SSP_LOG_LEVEL="${SSP_LOG_LEVEL:-INFO}"
# Works both from an editable install and straight from the source tree.
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"

exec python3 -m uvicorn ssp.api.app:app --host 127.0.0.1 --port "${PORT:-8000}" "$@"
