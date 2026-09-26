#!/usr/bin/env bash
# Runs the sample request sequence against a server on localhost:8080.
# Start the server first:  mvn -q compile exec:java  (or: java -cp ... com.example.vercov.Main)
set -u
BASE="${BASE_URL:-http://localhost:8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"

step() { printf '\n=== %s ===\n' "$1"; }

step "GET /api/meta (tzdb version)"
curl -s "$BASE/api/meta"

step "POST /api/versions base-v1 (priority 1, [0,10))"
curl -s -X POST -H 'Content-Type: application/json' --data @"$HERE/01-create-base.json" "$BASE/api/versions"

step "POST /api/versions patch-v2 (priority 2, [3,6))"
curl -s -X POST -H 'Content-Type: application/json' --data @"$HERE/02-create-patch.json" "$BASE/api/versions"

step "GET /api/coverage?from=0&to=10 (expect 3 non-overlapping segments)"
curl -s "$BASE/api/coverage?from=0&to=10"

step "GET /api/point?at=4 (expect patch-v2)"
curl -s "$BASE/api/point?at=4"

step "POST /api/versions clash-v3 (same-priority overlap, expect HTTP 409)"
curl -s -w '\nHTTP %{http_code}\n' -X POST -H 'Content-Type: application/json' \
  --data @"$HERE/03-create-conflict.json" "$BASE/api/versions"

step "DELETE /api/versions/patch-v2"
curl -s -X DELETE "$BASE/api/versions/patch-v2"

step "GET /api/coverage?from=0&to=10 after delete (expect single base-v1 segment)"
curl -s "$BASE/api/coverage?from=0&to=10"

step "POST /api/versions utc-day-2026-01-01 (time axis, epoch seconds)"
curl -s -X POST -H 'Content-Type: application/json' --data @"$HERE/04-create-time-axis.json" "$BASE/api/versions"

step "GET /api/point?at=1767225600 (2026-01-01T00:00:00Z, expect utc-day-2026-01-01)"
curl -s "$BASE/api/point?at=1767225600"

step "GET /api/point?at=1767312000 (exclusive end, expect null)"
curl -s "$BASE/api/point?at=1767312000"
echo
