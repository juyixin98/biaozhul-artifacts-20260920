#!/usr/bin/env bash
# Exact rational matrix service - local development launcher.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
export RATIONALSVC_RUN_LOG="${RATIONALSVC_RUN_LOG:-run_log.jsonl}"
exec uvicorn rationalsvc.api:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" "$@"
