#!/usr/bin/env bash
# End-to-end acceptance demo driven over real HTTP with curl.
# Usage: ./examples/demo.sh [baseUrl]   (default http://127.0.0.1:8099)
set -euo pipefail
BASE="${1:-http://127.0.0.1:8099}"

post() { # path json
  curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"
}

echo "=== 0. reset to a known-empty state ==="
post /reset '{}'
echo

echo "=== 1. bilateral inserts: 2 products (both 'books'), 2 order lines ==="
post /events '{"eventId":"e-p1","side":"product","op":"upsert","product":{"productId":"P1","category":"books"}}'
echo
post /events '{"eventId":"e-p2","side":"product","op":"upsert","product":{"productId":"P2","category":"books"}}'
echo
post /events '{"eventId":"e-l1","side":"order_line","op":"upsert","orderLine":{"orderLineId":"L1","productId":"P1","qty":2,"amount":"10.00"}}'
echo
post /events '{"eventId":"e-l2","side":"order_line","op":"upsert","orderLine":{"orderLineId":"L2","productId":"P2","qty":1,"amount":"5.00"}}'
echo

echo "=== 2. duplicate business event: same eventId e-l1 replayed verbatim ==="
post /events '{"eventId":"e-l1","side":"order_line","op":"upsert","orderLine":{"orderLineId":"L1","productId":"P1","qty":2,"amount":"10.00"}}'
echo

echo "=== 3. duplicate eventId carrying a *different* payload (still ignored) ==="
post /events '{"eventId":"e-l1","side":"order_line","op":"upsert","orderLine":{"orderLineId":"L9","productId":"P1","qty":99,"amount":"999.99"}}'
echo

echo "=== 4. dimension category change: P1 books -> media (L1 migrates, L2 stays) ==="
post /events '{"eventId":"e-p1-recat","side":"product","op":"upsert","product":{"productId":"P1","category":"media"}}'
echo

echo "=== 5. delete non-existent rows (safe no-op; notFound=true, eventId still consumed) ==="
post /events '{"eventId":"e-del-ghost-line","side":"order_line","op":"delete","orderLine":{"orderLineId":"NO-SUCH-LINE"}}'
echo
post /events '{"eventId":"e-del-ghost-prod","side":"product","op":"delete","product":{"productId":"NO-SUCH-PROD"}}'
echo

echo "=== 6. incremental view vs independent full recompute ==="
curl -s "$BASE/verify"
echo

echo "=== 7. real delete: remove L2 -> books bucket must disappear ==="
post /events '{"eventId":"e-del-l2","side":"order_line","op":"delete","orderLine":{"orderLineId":"L2"}}'
echo

echo "=== 8. final view and raw tables ==="
curl -s "$BASE/view"
echo
curl -s "$BASE/tables"
echo

echo "=== 9. batch with an embedded duplicate eventId (b-2 appears twice) ==="
post /events/batch '{"events":[
  {"eventId":"b-1","side":"product","op":"upsert","product":{"productId":"B1","category":"food"}},
  {"eventId":"b-2","side":"order_line","op":"upsert","orderLine":{"orderLineId":"BL1","productId":"B1","qty":1,"amount":"0.10"}},
  {"eventId":"b-2","side":"order_line","op":"upsert","orderLine":{"orderLineId":"BL1","productId":"B1","qty":1,"amount":"0.10"}},
  {"eventId":"b-3","side":"order_line","op":"upsert","orderLine":{"orderLineId":"BL2","productId":"B1","qty":1,"amount":"0.20"}}
]}'
echo
echo "=== food total must be exactly 0.30 (fixed point), qty 2 ==="
curl -s "$BASE/view"
echo
