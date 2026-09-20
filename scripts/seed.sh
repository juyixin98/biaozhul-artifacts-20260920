#!/usr/bin/env bash
# Seed a demo store with menu items, a published version, a temporary price
# window and a screen. Requires the API to be running (default :8123).
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8123}"
ADMIN="${ADMIN_TOKEN:-dev-admin-token}"
AUTH=(-H "Authorization: Bearer ${ADMIN}" -H "Content-Type: application/json")

echo "== create store =="
STORE=$(curl -sf "${AUTH[@]}" -X POST "$BASE/v1/stores" \
  -d '{"name":"Demo Downtown","timezone":"Asia/Shanghai"}')
STORE_ID=$(echo "$STORE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "store_id=$STORE_ID"

echo "== batch import menu items =="
curl -sf "${AUTH[@]}" -X POST "$BASE/v1/stores/$STORE_ID/items/batch-import" -d '{
  "items": [
    {"name":"Classic Burger","category":"Mains","price_cents":3200,"sold_out_threshold":50,"position":1},
    {"name":"Cheese Burger","category":"Mains","price_cents":3600,"sold_out_threshold":50,"position":2},
    {"name":"Fries","category":"Sides","price_cents":1200,"sold_out_threshold":100,"position":3},
    {"name":"Cola","category":"Drinks","price_cents":600,"position":4},
    {"name":"Latte","category":"Drinks","price_cents":1500,"position":5}
  ]}' > /dev/null

ITEM_ID=$(curl -sf "${AUTH[@]}" "$BASE/v1/stores/$STORE_ID/items" \
  | python3 -c 'import sys,json;print([i["id"] for i in json.load(sys.stdin) if i["name"]=="Latte"][0])')

echo "== publish version 1 =="
curl -sf "${AUTH[@]}" -X POST "$BASE/v1/stores/$STORE_ID/publish" \
  -d '{"expected_version":0}'
echo

echo "== happy-hour temp price for Latte (today 14:00-17:00 +08:00) =="
curl -sf "${AUTH[@]}" -X POST "$BASE/v1/stores/$STORE_ID/temp-prices" -d "{
  \"item_id\":\"$ITEM_ID\",\"price_cents\":990,
  \"starts_at\":\"$(date +%F)T14:00:00+08:00\",\"ends_at\":\"$(date +%F)T17:00:00+08:00\"}"
echo

echo "== create screen =="
SCREEN=$(curl -sf "${AUTH[@]}" -X POST "$BASE/v1/stores/$STORE_ID/screens" -d '{"name":"Front Counter 1"}')
TOKEN=$(echo "$SCREEN" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
echo "screen token: $TOKEN"

echo "== screen fetches menu (note the ETag) =="
curl -s -D - -o /dev/null -H "Authorization: Bearer $TOKEN" "$BASE/v1/screen/menu" | grep -iE 'HTTP|ETag'
echo
echo "Done. Try:"
echo "  curl -H 'Authorization: Bearer $TOKEN' $BASE/v1/screen/menu"
echo "  curl -X POST -H 'Authorization: Bearer $TOKEN' $BASE/v1/screen/heartbeat"
