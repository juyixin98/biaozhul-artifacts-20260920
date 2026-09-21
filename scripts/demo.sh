#!/usr/bin/env bash
# End-to-end ClearSettle demo against a running API.
#
#   ./scripts/demo.sh [base_url]
#
# Requires curl and jq. Starts nothing itself: run `docker compose up -d postgres`
# and `go run ./cmd/api` first.
set -euo pipefail

BASE="${1:-http://localhost:8080}"
ADMIN_EMAIL="${BOOTSTRAP_ADMIN_EMAIL:-admin@clearsettle.local}"
ADMIN_PASSWORD="${BOOTSTRAP_ADMIN_PASSWORD:-admin12345}"
TAG="demo-$(date +%s)"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing dependency: $1" >&2; exit 1; }; }
need curl; need jq

api() { curl -fsS "$BASE$1" "${@:2}"; }

echo "== health =="
api /healthz

echo
echo "== admin login =="
ADMIN_TOKEN=$(curl -fsS -XPOST "$BASE/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" | jq -r .token)
echo "admin token: ${ADMIN_TOKEN:0:18}…"

echo
echo "== create merchant =="
MERCHANT=$(curl -fsS -XPOST "$BASE/v1/admin/merchants" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"Demo $TAG\",\"fee_bps\":290,\"fee_fixed\":30}")
echo "$MERCHANT" | jq '{id: .merchant.id, name: .merchant.name, api_key_mask: .merchant.api_key_mask}'
MID=$(echo "$MERCHANT" | jq -r .merchant.id)
APIKEY=$(echo "$MERCHANT" | jq -r .api_key)

echo
echo "== create + log in operator =="
OPS_EMAIL="ops-$TAG@coffee.test"
OPS_PASS="operator-password"
curl -fsS -XPOST "$BASE/v1/admin/users" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$OPS_EMAIL\",\"password\":\"$OPS_PASS\",\"role\":\"operator\",\"merchant_id\":\"$MID\"}" >/dev/null
OP_TOKEN=$(curl -fsS -XPOST "$BASE/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$OPS_EMAIL\",\"password\":\"$OPS_PASS\"}" | jq -r .token)
echo "operator: $OPS_EMAIL"

h_api=(-H "Authorization: Bearer $APIKEY" -H 'Content-Type: application/json')
h_op=(-H "Authorization: Bearer $OP_TOKEN" -H 'Content-Type: application/json')

echo
echo "== authorize 4000 =="
P=$(curl -fsS -XPOST "$BASE/v1/payments/authorize" "${h_api[@]}" \
  -H "Idempotency-Key: $TAG-auth" -d '{"amount":4000,"external_ref":"'"$TAG"'"}')
PID=$(echo "$P" | jq -r .id)
echo "$P" | jq '{id, status, authorized_amount}'

echo
echo "== idempotent replay (same key, same body) =="
curl -fsS -D - -o /tmp/cs-replay.json -XPOST "$BASE/v1/payments/authorize" "${h_api[@]}" \
  -H "Idempotency-Key: $TAG-auth" -d '{"amount":4000,"external_ref":"'"$TAG"'"}' \
  | grep -i 'Idempotent-Replay' || true
test "$(jq -r .id /tmp/cs-replay.json)" = "$PID" && echo "replayed original payment $PID"

echo
echo "== capture =="
curl -fsS -XPOST "$BASE/v1/payments/$PID/capture" "${h_api[@]}" \
  -H "Idempotency-Key: $TAG-cap" -d '{}' | jq '{status, captured_amount, fee_amount}'

echo
echo "== partial refund 1000 =="
curl -fsS -XPOST "$BASE/v1/payments/$PID/refund" "${h_api[@]}" \
  -H "Idempotency-Key: $TAG-ref1" -d '{"amount":1000,"reason":"demo"}' \
  | jq '{refund: .refund.amount, fee_refund: .refund.fee_refund, status: .payment.status}'

echo
echo "== over-refund rejected =="
if curl -fsS -XPOST "$BASE/v1/payments/$PID/refund" "${h_api[@]}" \
  -H "Idempotency-Key: $TAG-ref-bad" -d '{"amount":3001}'; then
  echo "ERROR: over-refund was accepted" >&2; exit 1
else
  echo "correctly rejected with 422"
fi

echo
echo "== balance =="
curl -fsS "$BASE/v1/accounts/balance" "${h_op[@]}" | jq .

echo
echo "== audit log (admin) =="
curl -fsS "$BASE/v1/audit-logs?limit=5" -H "Authorization: Bearer $ADMIN_TOKEN" \
  | jq '{count: (.audit_logs|length), latest: .audit_logs[0].action}'

echo
echo "Demo complete. Merchant=$MID payment=$PID"
