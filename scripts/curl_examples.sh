#!/usr/bin/env bash
# Example HTTP requests. Start the server first:
#   uvicorn app.main:app --reload --port 8000
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "== health =="
curl -s "$BASE/health" | python3 -m json.tool

echo "== signing key =="
curl -s "$BASE/signing-key" | python3 -m json.tool

echo "== dangerous sample =="
curl -s -X POST "$BASE/analyze" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c '
import json
print(json.dumps({"code": open("examples/dangerous.tl").read()}))')" \
  | python3 -m json.tool

echo "== safe sample =="
curl -s -X POST "$BASE/analyze" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c '
import json
print(json.dumps({"code": open("examples/safe_sanitized.tl").read()}))')" \
  | python3 -m json.tool
