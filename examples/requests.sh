#!/usr/bin/env bash
# Example HTTP requests for the taint analysis service.
# Usage: ./examples/requests.sh            # server must be running
#        PORT=9000 ./examples/requests.sh
set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
BASE="http://${HOST}:${PORT}"

echo "== health =="
curl -s "${BASE}/health"; echo

echo "== public key (Ed25519, PEM) =="
curl -s "${BASE}/public-key"

echo "== dangerous sample: source -> sink =="
curl -s -X POST "${BASE}/analyze" \
  -H 'Content-Type: application/json' \
  -d '{"code": "x = source();\nsink(x);\n"}'
echo

echo "== safe sample: sanitized =="
curl -s -X POST "${BASE}/analyze" \
  -H 'Content-Type: application/json' \
  -d '{"code": "x = source();\nx = sanitize(x);\nsink(x);\n"}'
echo

echo "== cross-function fixture from file =="
jq -Rs '{code: .}' tests/fixtures/cross_function.tl \
  | curl -s -X POST "${BASE}/analyze" -H 'Content-Type: application/json' -d @-
echo

echo "== parse error =="
curl -s -X POST "${BASE}/analyze" \
  -H 'Content-Type: application/json' \
  -d '{"code": "x = ;"}'
echo
