#!/usr/bin/env bash
# Start the CSP HTTP service locally.
set -euo pipefail
cd "$(dirname "$0")/.."
export CSP_DATABASE_PATH="${CSP_DATABASE_PATH:-csp_evidence.db}"
exec python3 -m uvicorn app.api.app:app --host "${CSP_HOST:-127.0.0.1}" --port "${CSP_PORT:-8000}"
