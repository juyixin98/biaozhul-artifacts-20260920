#!/usr/bin/env bash
# Manual smoke demo: walks pages, mutates data mid-walk, shows snapshot
# stability, and demonstrates tampered/expired cursor rejection.
# Usage: ./examples.sh [base_url]   (default http://localhost:8080)
set -euo pipefail
BASE="${1:-http://localhost:8080}"

say() { printf '\n=== %s ===\n' "$1"; }

say "health"
curl -sS "$BASE/healthz"; echo

say "page 1 (limit=5, default sort score asc)"
RESP=$(curl -sS "$BASE/api/items?limit=5")
echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"
CURSOR=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["nextCursor"])')

say "page 2 using the signed cursor"
curl -sS --get "$BASE/api/items" --data-urlencode "limit=5" --data-urlencode "cursor=$CURSOR" \
  | python3 -m json.tool 2>/dev/null || true

say "insert + delete + score change WHILE paging (fresh walk)"
RESP=$(curl -sS "$BASE/api/items?limit=10")
CURSOR=$(echo "$RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin)["nextCursor"])')
echo "snapshot pinned; now mutating live data..."
curl -sS -X POST "$BASE/api/items" -H 'Content-Type: application/json' \
  -d '{"id":"demo-x","name":"Demo X","category":"books","score":33}' >/dev/null
curl -sS -X PATCH "$BASE/api/items/a-009" -H 'Content-Type: application/json' -d '{"score":51}' >/dev/null
curl -sS -X DELETE "$BASE/api/items/a-020" >/dev/null
echo "continuation still walks the OLD snapshot (no demo-x, a-009 still score 50, a-020 still present):"
curl -sS --get "$BASE/api/items" --data-urlencode "limit=10" --data-urlencode "cursor=$CURSOR" \
  | python3 -m json.tool 2>/dev/null || true
echo "cleanup..."
curl -sS -X DELETE "$BASE/api/items/demo-x" >/dev/null
curl -sS -X PATCH "$BASE/api/items/a-009" -H 'Content-Type: application/json' -d '{"score":50}' >/dev/null
curl -sS -X POST "$BASE/api/items" -H 'Content-Type: application/json' \
  -d '{"id":"a-020","name":"Tango","category":"music","score":150}' >/dev/null

say "change the filter but reuse the old cursor -> 400 QUERY_MISMATCH"
curl -sS -o /tmp/r.json -w "HTTP %{http_code}\n" --get "$BASE/api/items" \
  --data-urlencode "category=books" --data-urlencode "limit=5" --data-urlencode "cursor=$CURSOR"
cat /tmp/r.json; echo

say "forged cursor (flip a byte) -> 400 CURSOR_INVALID"
FORGED="${CURSOR/x/y}"
curl -sS -o /tmp/r.json -w "HTTP %{http_code}\n" --get "$BASE/api/items" \
  --data-urlencode "limit=5" --data-urlencode "cursor=$FORGED"
cat /tmp/r.json; echo

say "filter + descending sort example"
curl -sS "$BASE/api/items?category=music&sort=name&order=desc&limit=3" \
  | python3 -m json.tool 2>/dev/null || true
