#!/usr/bin/env bash
# Request examples against the local analysis service.
# Start the server first:  python -m abtest.server --port 8000
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

echo "== GET /health =="
curl -s "${BASE_URL}/health"
echo

echo "== POST /analyze (fixed-sample mean-difference CI) =="
curl -s -X POST "${BASE_URL}/analyze" \
  -H "Content-Type: application/json" \
  --data @"$(dirname "$0")/analyze_request.json"
echo

echo "== POST /analyze (invalid: group too small -> HTTP 400) =="
curl -s -X POST "${BASE_URL}/analyze" \
  -H "Content-Type: application/json" \
  --data '{"control": [1.0], "treatment": [1.0, 2.0]}'
echo
