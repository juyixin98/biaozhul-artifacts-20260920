#!/usr/bin/env bash
# Example HTTP session. Start the service first:
#   ./build/bipartite-matching serve --port 18080
# Then run this script.
set -euo pipefail

PORT="${1:-18080}"
BASE="http://127.0.0.1:${PORT}"

echo "== health =="
curl -s "${BASE}/health"
echo

echo "== solve (with naive brute-force cross-check) =="
curl -s -X POST "${BASE}/solve" \
  -H 'Content-Type: application/json' \
  -d '{
    "left": [1, 2, 3, 10],
    "right": [7, 8, 9],
    "edges": [
      {"left": 1, "right": 7},
      {"left": 1, "right": 8},
      {"left": 2, "right": 8},
      {"left": 3, "right": 7},
      {"left": 3, "right": 9},
      {"left": 3, "right": 9}
    ],
    "brute_force": true
  }'
echo

echo "== malformed JSON (expect HTTP 400) =="
curl -s -X POST "${BASE}/solve" -d '{bad' -w '\n[HTTP %{http_code}]\n'
