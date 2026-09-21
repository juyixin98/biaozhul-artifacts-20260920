#!/usr/bin/env bash
# examples/demo.sh — full ClearSettle simulated-transaction walkthrough.
#
# Usage:
#   docker compose up --build        # terminal 1 (prints the admin key)
#   ./examples/demo.sh              # terminal 2
#
# Requires: curl, jq. Creates its own merchant, so it is safe to re-run.
set -euo pipefail

BASE=${BASE:-http://localhost:8080}
ADMIN=${CLEARSETTLE_ADMIN_KEY:-sk_ad_demoadminkey000000000000000000}
DATE=${DATE:-$(date -u +%F)}

need() { command -v "$1" >/dev/null || { echo "missing dependency: $1" >&2; exit 1; }; }
need curl; need jq

post() { # post <key> <idem> <path> <json>
  curl -sS -X POST "$BASE$3" \
    -H "Authorization: Bearer $1" -H "Idempotency-Key: $2" \
    -H 'Content-Type: application/json' -d "$4"
}

echo "▸ health"
curl -sS "$BASE/healthz" | jq .

echo "▸ create merchant (admin)"
merchant=$(post "$ADMIN" "demo-mk-$RANDOM" /v1/admin/merchants \
  '{"name":"Demo Storefront Ltd"}')
echo "$merchant" | jq '{merchant_id, name, fee_bps, fee_fixed_cents}'
MID=$(echo "$merchant" | jq -r .merchant_id)
OP=$(echo "$merchant" | jq -r .operator_key)
echo "  operator key issued once: ${OP:0:12}…"

echo "▸ authorize \$50.00"
auth=$(post "$OP" "demo-auth-$RANDOM" /v1/payments/authorize \
  "{\"merchant_id\":\"$MID\",\"amount_cents\":5000,\"card_last4\":\"4242\"}")
PID=$(echo "$auth" | jq -r .id)
echo "$auth" | jq '{id,status,card,auth_expires_at}'

echo "▸ capture (fee = 2.9% of 5000 = 145, +30 = 175)"
CAP_KEY="demo-cap-$RANDOM"
post "$OP" "$CAP_KEY" /v1/payments/capture \
  "{\"payment_id\":\"$PID\"}" | jq '{status,captured_cents,fee_cents}'

echo "▸ same capture key + different amount -> 409 conflict"
post "$OP" "$CAP_KEY" /v1/payments/capture \
  "{\"payment_id\":\"$PID\",\"capture_cents\":4000}" | jq -c '{conflict: .error}'

echo "▸ partial refund \$20.00 (proportional fee release 58c)"
REF_KEY="demo-ref-$RANDOM"
ref=$(post "$OP" "$REF_KEY" /v1/payments/refund \
  "{\"payment_id\":\"$PID\",\"amount_cents\":2000,\"reason\":\"customer return\"}")
echo "$ref" | jq '{amount_cents,fee_refund_cents,net_cents}'
RID=$(echo "$ref" | jq -r .id)

echo "▸ duplicate refund (same key/params) replays the original, posts nothing"
post "$OP" "$REF_KEY" /v1/payments/refund \
  "{\"payment_id\":\"$PID\",\"amount_cents\":2000,\"reason\":\"customer return\"}" \
  | jq -c --arg rid "$RID" '{replayed_refund: .id, matches_original: (.id == $rid)}'

echo "▸ ledger entries for the payment (balanced, append-only)"
curl -sS "$BASE/v1/ledger/payment/$PID" -H "Authorization: Bearer $OP" \
  | jq '.entries[] | {account_code, amount_cents}'

echo "▸ settle for $DATE"
curl -sS -X POST "$BASE/v1/merchants/$MID/settle?date=$DATE" \
  -H "Authorization: Bearer $ADMIN" \
  | jq '{batch_date,status,total_cents,fee_cents,net_cents,items_settled}'

echo "▸ settle again -> already_existed (idempotent)"
curl -sS -X POST "$BASE/v1/merchants/$MID/settle?date=$DATE" \
  -H "Authorization: Bearer $ADMIN" | jq -c '{status, already_existed}'

echo "▸ sync simulated gateway feed and reconcile (clean)"
post "$ADMIN" "demo-sync-$RANDOM" /v1/admin/statements/sync \
  "{\"merchant_id\":\"$MID\",\"as_of\":\"$DATE\"}" | jq -c .
curl -sS -X POST "$BASE/v1/merchants/$MID/reconcile?date=$DATE" \
  -H "Authorization: Bearer $ADMIN" \
  | jq '{status, captured_cents, refunded_cents, gateway_cash_cents, payable_cents, diff_cents, discrepancies_count}'

echo "▸ corrupt the capture statement by 100c and reconcile (new date)"
BAD_DATE=$(date -u -d "+1 day" +%F 2>/dev/null || date -u -v+1d +%F)
post "$ADMIN" "demo-stmt-$RANDOM" /v1/admin/statements \
  "{\"merchant_id\":\"$MID\",\"ref_id\":\"$PID\",\"kind\":\"capture\",\"amount_cents\":4900,\"stmt_date\":\"$DATE\"}" >/dev/null
curl -sS -X POST "$BASE/v1/merchants/$MID/reconcile?date=$BAD_DATE" \
  -H "Authorization: Bearer $ADMIN" \
  | jq '{run_date, discrepancies_count, first: .discrepancies[0] | {kind,diff_cents,detail}}'

echo "▸ auditor is read-only (403)"
AUD=$(post "$ADMIN" "demo-aud-$RANDOM" /v1/admin/keys \
  "{\"role\":\"auditor\",\"merchant_id\":\"$MID\"}" | jq -r .api_key)
post "$AUD" "demo-denied-$RANDOM" /v1/payments/authorize \
  "{\"merchant_id\":\"$MID\",\"amount_cents\":1,\"card_last4\":\"0000\"}" \
  | jq -c '{forbidden: .error}'

echo "▸ audit trail"
curl -sS "$BASE/v1/audit?limit=10" -H "Authorization: Bearer $OP" \
  | jq '.events[] | {action,target_type,created_at}'

echo "✅ demo complete"
