#!/usr/bin/env bash
# End-to-end demo against a locally running server.
# Usage: ./examples/curl-demo.sh [base-url]   (default http://localhost:8080)
set -euo pipefail

BASE="${1:-http://localhost:8080}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

post_file() { # $1 = json file
  curl -sS -X POST "$BASE/api/traces" \
    -H 'Content-Type: application/json' \
    --data-binary @"$1"
  echo
}

echo "== 1) health =="
curl -sS "$BASE/healthz"; echo

echo "== 2) ingest serial/parallel trace =="
post_file "$DIR/requests/01-ingest-serial-parallel.json"

echo "== 3) analyze critical path (expect 140) =="
curl -sS "$BASE/api/traces/trace-serial-parallel/critical" | python3 -m json.tool

echo "== 4) ingest overlapping sync children =="
post_file "$DIR/requests/02-ingest-sync-overlap.json"
curl -sS "$BASE/api/traces/trace-sync-overlap/critical" | python3 -m json.tool

echo "== 5) missing span =="
post_file "$DIR/requests/03-ingest-missing-span.json"
curl -sS "$BASE/api/traces/trace-missing-span/critical" | python3 -m json.tool

echo "== 6) cyclic input (expect HTTP 422) =="
post_file "$DIR/requests/04-ingest-cycle.json"
curl -sS -o /tmp/cycle.json -w "HTTP %{http_code}\n" "$BASE/api/traces/trace-cycle/critical"
python3 -m json.tool /tmp/cycle.json

echo "== 7) clock skew =="
post_file "$DIR/requests/05-ingest-clock-skew.json"
curl -sS "$BASE/api/traces/trace-clock-skew/critical" | python3 -m json.tool

echo "== 8) built-in synthetic seeding =="
for name in serial_parallel sync_overlap missing_span cycle clock_skew; do
  curl -sS -X POST "$BASE/api/synthetic/seed?name=$name" >/dev/null
  echo "seeded $name"
done

echo "== 9) list traces =="
curl -sS "$BASE/api/traces" | python3 -m json.tool
