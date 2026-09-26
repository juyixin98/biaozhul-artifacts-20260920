#!/usr/bin/env bash
# Manual acceptance walkthrough against a running idemresp server.
# Usage: scripts/manual-demo.sh [APP_BASE] [GW_BASE]
set -u
APP="${1:-http://127.0.0.1:29080}"
GW="${2:-http://127.0.0.1:29081}"
BODY='{"amount":1000,"currency":"USD","reference":"invoice-42"}'

line() { printf '\n========== %s ==========\n' "$1"; }
code() { curl -sS -o /tmp/idemrun/body -w '%{http_code}' "$@"; }

line "0. reset gateway state"
curl -sS -X POST "$GW/gateway/reset"; echo

line "1. create order (fresh key)"
K="manual-$(date +%s)"
echo "key=$K"
curl -sS -D /tmp/idemrun/h1 -o /tmp/idemrun/b1 -w 'HTTP %{http_code}\n' \
  -X POST "$APP/v1/orders" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $K" -d "$BODY"
grep -i 'Idempotent-Replay' /tmp/idemrun/h1 || echo "(no replay header — correct for first call)"
cat /tmp/idemrun/b1; echo

line "2. retry same key + same body -> replay, identical response"
cp /tmp/idemrun/b1 /tmp/idemrun/b1.first
curl -sS -D /tmp/idemrun/h2 -o /tmp/idemrun/b2 -w 'HTTP %{http_code}\n' \
  -X POST "$APP/v1/orders" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $K" -d "$BODY"
grep -i 'Idempotent-Replay' /tmp/idemrun/h2
if cmp -s /tmp/idemrun/b1.first /tmp/idemrun/b2; then echo "BODY IDENTICAL: yes"; else echo "BODY IDENTICAL: NO"; fi

line "3. same key, DIFFERENT body -> 409 conflict"
curl -sS -w '\nHTTP %{http_code}\n' \
  -X POST "$APP/v1/orders" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $K" -d '{"amount":9999,"currency":"USD"}'

line "4. ledger side effects for key (want count=1)"
curl -sS "$APP/v1/ledger?key=$K" | head -c 600; echo

line "5. gateway charges (want 1)"
curl -sS "$GW/gateway/metrics"; echo

line "6. ambiguous gateway result (reset AFTER charge), key forwarded"
KA="${K}-ambig"
curl -sS -w '\nHTTP %{http_code}\n' \
  -X POST "$APP/v1/orders" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KA" -H "X-Fault: reset-after-charge" -d "$BODY"

line "7. retry ambiguous key -> replay; still one ledger / one gateway charge"
curl -sS -D - -o /tmp/idemrun/b7 -w 'HTTP %{http_code}\n' \
  -X POST "$APP/v1/orders" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KA" -d "$BODY" | grep -iE 'HTTP|Idempotent-Replay'
echo "ledger:"; curl -sS "$APP/v1/ledger?key=$KA"; echo
echo "gateway charges now:"; curl -sS "$GW/gateway/metrics"; echo

line "8. keyless ambiguity -> 503, NO local side effect (boundary demo)"
KB="${K}-keyless"
curl -sS -w '\nHTTP %{http_code}\n' \
  -X POST "$APP/v1/orders" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KB" -H "X-Forward-Key: false" \
  -H "X-Fault: reset-after-charge" -d "$BODY"
echo "local ledger (want count=0):"; curl -sS "$APP/v1/ledger?key=$KB"; echo
echo "gateway charges (invisible external effect, want 1 more):"; curl -sS "$GW/gateway/metrics"; echo

line "DONE key=$K"
