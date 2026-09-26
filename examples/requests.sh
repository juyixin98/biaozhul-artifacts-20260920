#!/usr/bin/env bash
# Request samples for the idemresp service.
#
# Prerequisite: a server is running, e.g.
#   go run ./cmd/idemresp -addr :18080 -gateway-addr :18081 -data-dir ./data
#
# Every side-effecting request carries an Idempotency-Key header.
# Re-run any command unchanged: the second call is a REPLAY (same stored
# response, Idempotent-Replay: true) and creates no second side effect.
#
# Override APP/GW if you started on non-default ports.
APP="${APP:-http://127.0.0.1:18080}"
GW="${GW:-http://127.0.0.1:18081}"

# A fixed key makes the samples repeatable. Use your own unique key in real
# clients (e.g. a UUID generated per logical request, NOT per HTTP attempt).
KEY="sample-order-0001"
BODY='{"amount":1000,"currency":"USD","reference":"invoice-42"}'

echo "## 1. Create an order (first call -> 201, executes once)"
curl -sS -i -X POST "$APP/v1/orders" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d "$BODY"
echo

echo "## 2. Retry unchanged (-> 201, Idempotent-Replay: true, identical body)"
curl -sS -i -X POST "$APP/v1/orders" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d "$BODY"
echo

echo "## 3. Same key, DIFFERENT body (-> 409 idempotency_key_conflict)"
curl -sS -i -X POST "$APP/v1/orders" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d '{"amount":9999,"currency":"USD"}'
echo

echo "## 4. Concurrent duplicate while the first is still processing"
echo "##    (-> 409 request_in_progress + Retry-After; then retries replay)"
curl -sS -i -X POST "$APP/v1/orders" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: sample-slow-0001" \
  -H "X-Pre-Commit-Delay: 1500ms" \
  -d "$BODY" &
sleep 0.2
curl -sS -i -X POST "$APP/v1/orders" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: sample-slow-0001" \
  -d "$BODY"
wait
echo

echo "## 5. Inspect the stored idempotency record"
curl -sS -i "$APP/v1/keys/$KEY"
echo

echo "## 6. Count committed side effects for the key (must be 1)"
curl -sS -i "$APP/v1/ledger?key=$KEY"
echo

echo "## 7. Fault injection: ambiguous gateway call (reset AFTER charge)."
echo "##    The end-to-end key is forwarded, the gateway dedupes, still ONE charge."
curl -sS -i -X POST "$APP/v1/orders" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: sample-ambig-0001" \
  -H "X-Fault: reset-after-charge" \
  -d "$BODY"
echo

echo "## 8. Boundary demo: ambiguous call WITHOUT forwarding the key."
echo "##    -> 503 ambiguous_outcome, no LOCAL side effect; the external"
echo "##    gateway may already have charged. Exactly-once is NOT guaranteed"
echo "##    for arbitrary external calls without end-to-end idempotency."
curl -sS -i -X POST "$APP/v1/orders" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: sample-keyless-0001" \
  -H "X-Forward-Key: false" \
  -H "X-Fault: reset-after-charge" \
  -d "$BODY"
echo

echo "## 9. Gateway-side truth: how many charges actually happened"
curl -sS -i "$GW/gateway/metrics"
echo
