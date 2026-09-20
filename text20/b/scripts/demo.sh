#!/usr/bin/env bash
# End-to-end smoke walkthrough against a running server.
# Usage: BASE=http://localhost:8080 KEY=dev-management-key ./scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
KEY="${KEY:-dev-management-key}"
MGMT=(-H "Authorization: Bearer $KEY" -H 'Content-Type: application/json')

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

say "create store (NYC)"
STORE=$(curl -s "${MGMT[@]}" -X POST "$BASE/v1/stores" \
  -d '{"name":"Demo NYC","timezone":"America/New_York"}')
echo "$STORE"
SID=$(echo "$STORE" | sed -E 's/.*"id":([0-9]+).*/\1/')

say "create dishes"
D1=$(curl -s "${MGMT[@]}" -X POST "$BASE/v1/stores/$SID/dishes" -d '{"name":"Burger","base_price":999}')
D2=$(curl -s "${MGMT[@]}" -X POST "$BASE/v1/stores/$SID/dishes" -d '{"name":"Fries","base_price":399}')
echo "$D1"; echo "$D2"
ID1=$(echo "$D1" | sed -E 's/.*"id":([0-9]+).*/\1/')
ID2=$(echo "$D2" | sed -E 's/.*"id":([0-9]+).*/\1/')

say "fries sell out after 50/day"
curl -s "${MGMT[@]}" -X PUT "$BASE/v1/stores/$SID/dishes/$ID2/threshold" -d '{"threshold":50}' -o /dev/null -w '%{http_code}\n'

say "draft + publish (expected_version 0)"
curl -s "${MGMT[@]}" -X PUT "$BASE/v1/stores/$SID/draft" \
  -d "{\"items\":[{\"dish_id\":$ID1},{\"dish_id\":$ID2}]}" >/dev/null
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl -s "${MGMT[@]}" -X POST "$BASE/v1/stores/$SID/publish" \
  -d "{\"expected_version\":0,\"temp_prices\":[{\"dish_id\":$ID2,\"price\":299,\"starts_at\":\"$NOW\",\"ends_at\":\"2030-01-01T00:00:00Z\"}]}"

say "register screen"
SCREEN=$(curl -s "${MGMT[@]}" -X POST "$BASE/v1/stores/$SID/screens" -d '{"name":"demo-screen"}')
echo "$SCREEN"
TOK=$(echo "$SCREEN" | sed -E 's/.*"token":"([^"]+)".*/\1/')

say "screen pulls menu (fries at temp price 299)"
curl -s -H "Authorization: Bearer $TOK" "$BASE/screen/v1/menu"

say "send 50 fries sales then re-check menu (sold_out flips)"
for i in $(seq 1 50); do
  curl -s "${MGMT[@]}" -X POST "$BASE/v1/stores/$SID/sales" \
    -d "{\"event_id\":\"demo-$i\",\"dish_id\":$ID2,\"quantity\":1,\"occurred_at\":\"$NOW\"}" >/dev/null
done
curl -s -H "Authorization: Bearer $TOK" "$BASE/screen/v1/menu"

say "duplicate event is ignored (demo-1)"
curl -s "${MGMT[@]}" -X POST "$BASE/v1/stores/$SID/sales" \
  -d "{\"event_id\":\"demo-1\",\"dish_id\":$ID2,\"quantity\":1,\"occurred_at\":\"$NOW\"}"

say "done"
