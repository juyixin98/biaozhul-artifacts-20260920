#!/usr/bin/env bash
# Independent unit + integration test suite. Logs are written under logs/test/.
set -euo pipefail
cd "$(dirname "$0")/.."

export SSP_DB_PATH="${SSP_DB_PATH:-data/test-runs.db}"
export SSP_LOG_DIR="${SSP_LOG_DIR:-logs/test}"
export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p logs

exec python3 -m pytest "$@"
