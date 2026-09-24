#!/usr/bin/env bash
# Reproduce the manual HTTP examples. Requires: a running server and curl.
#   java -cp build/classes com.example.diff.Main server 8080
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"

echo "== health =="
curl -s "$BASE/health"; echo

echo "== diff (normal, claims shortest) =="
curl -s -X POST "$BASE/api/diff" -H 'Content-Type: application/json' \
  -d @examples/diff-request.json | sed -n '1,12p'

echo "== diff (budget 1 forces degraded, still applicable) =="
curl -s -X POST "$BASE/api/diff" -H 'Content-Type: application/json' \
  -d @examples/diff-degraded-request.json | sed -n '1,8p'

echo "== search synthetic corpus =="
curl -s -X POST "$BASE/api/search" -H 'Content-Type: application/json' \
  -d @examples/search-request.json | sed -n '1,12p'

echo "== apply supplied edit script =="
curl -s -X POST "$BASE/api/apply" -H 'Content-Type: application/json' \
  -d @examples/apply-request.json; echo

echo "== malformed script -> 422 =="
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/api/apply" \
  -H 'Content-Type: application/json' \
  -d '{"old":"a\n","edits":[{"kind":"delete","oldStart":0,"oldEnd":1,"newStart":0,"newEnd":1,"oldLines":["a\n"],"newLines":[]}]}'
