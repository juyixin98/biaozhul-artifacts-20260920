#!/usr/bin/env bash
# One-command acceptance:
#   1. ensure local PostgreSQL is running (Docker)
#   2. provision an isolated acceptance database
#   3. verify locked dependencies, build, vet
#   4. run the full automated test suite (incl. real-subprocess crash/restart)
#   5. run the live HTTP "gap fill + exactly-once" scenario end to end
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${INBOX_PG_PORT:-55432}"
ACCEPT_DB="${INBOX_ACCEPT_DB:-inbox_accept}"
ADDR="127.0.0.1:8091"
BASE="http://$ADDR"

echo "==> [1/5] PostgreSQL"
./scripts/start-postgres.sh

echo "==> [2/5] provision isolated database '$ACCEPT_DB'"
docker exec inbox-pg psql -U inbox -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS $ACCEPT_DB" \
  -c "CREATE DATABASE $ACCEPT_DB" >/dev/null
DSN="postgres://inbox:inbox@localhost:$PORT/$ACCEPT_DB?sslmode=disable"

echo "==> [3/5] dependency / build / vet"
go mod verify
go build ./...
go vet ./...

echo "==> [4/5] full automated test suite"
INBOX_TEST_DSN="$DSN" go test -count=1 ./...

echo "==> [5/5] live HTTP scenario (out-of-order staging, gap fill, exactly-once)"
mkdir -p bin tmp
go build -o bin/inboxd     ./cmd/inboxd
go build -o bin/fixturetool ./cmd/fixturetool
rm -rf tmp/accept && mkdir -p tmp/accept
./bin/fixturetool generate tmp/accept >/dev/null

INBOX_DB="$DSN" INBOX_ADDR="$ADDR" INBOX_WORKER=off ./bin/inboxd >tmp/accept/server.log 2>&1 &
SRV_PID=$!
trap 'kill "$SRV_PID" >/dev/null 2>&1 || true' EXIT

for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null && break
  sleep 0.1
done

post() { # <json-file> <path>
  curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
       --data @"$1" "$BASE$2"
}
post_expect() { # <json-file> <path> <want-status>
  code="$(post "$1" "$2")"
  if [ "$code" != "$3" ]; then
    echo "POST $2 with $1 returned $code, want $3" >&2
    cat tmp/accept/server.log >&2 || true
    exit 1
  fi
}
delivered() {
  curl -s -X POST -H 'Content-Type: application/json' --data '{}' "$BASE/v1/process" \
    | jq -r '.delivered'
}

D="tmp/accept/basic"
post_expect "$D/open-1.json"   /v1/chains/chain-a/channels 201
post_expect "$D/header-1.json" /v1/headers 202
post_expect "$D/header-2.json" /v1/headers 202
post_expect "$D/header-3.json" /v1/headers 202
post_expect "$D/message-1.json" /v1/messages 202
post_expect "$D/message-3.json" /v1/messages 202
[ "$(delivered)" = "1" ] || { echo "nonce1 should deliver"; exit 1; }
post_expect "$D/message-2.json" /v1/messages 202
[ "$(delivered)" = "0" ] || { echo "nonce2 must wait for block confirmation"; exit 1; }
post_expect "$D/header-4.json" /v1/headers 202
[ "$(delivered)" = "1" ] || { echo "nonce2 should deliver after block4"; exit 1; }
post_expect "$D/header-5.json" /v1/headers 202
[ "$(delivered)" = "1" ] || { echo "nonce3 should deliver after block5"; exit 1; }
# Identical duplicate is idempotent and must not re-execute.
post_expect "$D/message-1.json" /v1/messages 202
[ "$(delivered)" = "0" ] || { echo "identical duplicate re-executed"; exit 1; }

DELIVERIES="$(curl -s "$BASE/v1/chains/chain-a/channels/orders/deliveries")"
echo "$DELIVERIES" | jq .
[ "$(echo "$DELIVERIES" | jq 'length')" = "3" ] || { echo "expected 3 deliveries"; exit 1; }
[ "$(echo "$DELIVERIES" | jq -c '[.[].attempts] | unique')" = "[1]" ] || {
  echo "every delivery must have attempts=1 (exactly-once)"; exit 1; }

echo
echo "ACCEPTANCE PASSED"
echo "- unit + integration + real crash/restart e2e: green"
echo "- live gap-fill delivered nonces [1,2,3] exactly once"
