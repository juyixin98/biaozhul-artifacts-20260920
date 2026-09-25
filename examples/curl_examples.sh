#!/usr/bin/env bash
# Example requests against a locally running abseq service.
# Start the service first:  python -m abseq.server --port 8000
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== GET /health =="
curl -s "${BASE}/health"
echo

echo "== POST /v1/analyze (two explicit samples) =="
curl -s -X POST "${BASE}/v1/analyze" \
  -H 'Content-Type: application/json' \
  --data @"${HERE}/analyze_request.json"
echo

echo "== POST /v1/analyze (group labels + missing values, drop strategy) =="
curl -s -X POST "${BASE}/v1/analyze" \
  -H 'Content-Type: application/json' \
  --data @"${HERE}/analyze_groups_request.json"
echo

echo "== POST /v1/coverage (Monte Carlo coverage under the null) =="
curl -s -X POST "${BASE}/v1/coverage" \
  -H 'Content-Type: application/json' \
  --data @"${HERE}/coverage_request.json"
echo

echo "== POST /v1/peeking (type-I inflation from sequential peeking) =="
curl -s -X POST "${BASE}/v1/peeking" \
  -H 'Content-Type: application/json' \
  --data @"${HERE}/peeking_request.json"
echo
