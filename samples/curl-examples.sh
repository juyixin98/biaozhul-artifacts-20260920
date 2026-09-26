#!/usr/bin/env bash
# Request samples against a locally running HLC backend (default port 8080).
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== GET /api/meta (runtime metadata incl. tzdb version) =="
curl -sS "$BASE/api/meta"
echo; echo

echo "== GET /api/now (current timestamp, does not advance the clock) =="
curl -sS "$BASE/api/now"
echo; echo

echo "== POST /api/send (local/send event) =="
curl -sS -X POST "$BASE/api/send" -H 'Content-Type: application/json' -d @"$HERE/send-request.json"
echo; echo

echo "== POST /api/receive (merge a remote timestamp) =="
curl -sS -X POST "$BASE/api/receive" -H 'Content-Type: application/json' -d @"$HERE/receive-request.json"
echo; echo

echo "== POST /api/receive with malformed body (expect ok=false) =="
curl -sS -X POST "$BASE/api/receive" -H 'Content-Type: application/json' -d 'not json'
echo
