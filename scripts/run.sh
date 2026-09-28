#!/usr/bin/env bash
# Start the exact rational linear algebra service.
set -euo pipefail

cd "$(dirname "$0")/.."

export PYTHONPATH="${PYTHONPATH:-}:src"
export RATIONAL_LINALG_DIGIT_BUDGET="${RATIONAL_LINALG_DIGIT_BUDGET:-4096}"
export RATIONAL_LINALG_DECIMAL_DPS="${RATIONAL_LINALG_DECIMAL_DPS:-40}"
export RATIONAL_LINALG_LOG_DIR="${RATIONAL_LINALG_LOG_DIR:-logs}"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

mkdir -p "$RATIONAL_LINALG_LOG_DIR"

echo "Starting rational-linalg on http://${HOST}:${PORT}"
echo "  digit budget : ${RATIONAL_LINALG_DIGIT_BUDGET}"
echo "  decimal dps  : ${RATIONAL_LINALG_DECIMAL_DPS}"
echo "  log dir      : ${RATIONAL_LINALG_LOG_DIR}"
exec python3 -m uvicorn rational_linalg.service.app:app \
  --host "$HOST" --port "$PORT" "$@"
