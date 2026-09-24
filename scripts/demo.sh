#!/usr/bin/env bash
# End-to-end acceptance demo for the fencing-token resource protection.
#
# Drives the two running HTTP services with curl, reproducing:
#   1. holder A gets token 1 and writes
#   2. A stays idle until its lease expires
#   3. holder B gets a larger token and writes
#   4. A resumes and retries with the stale token -> rejected
#   5. process restart: tokens keep increasing, stale writes stay rejected
#
# Usage: scripts/demo.sh [path-to-binary]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${1:-$ROOT_DIR/fencingdemo}"
DATA_DIR="$ROOT_DIR/data-demo"
LOCK_ADDR=":18080"
RES_ADDR=":18090"
LOCK_BASE="http://127.0.0.1:18080"
RES_BASE="http://127.0.0.1:18090"
TTL_SECONDS=3

command -v jq >/dev/null || { echo "jq is required for this script" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl is required for this script" >&2; exit 1; }

PASS=0; FAIL=0
check() { # check <description> <test args...>
  local desc="$1"; shift
  if test "$@"; then
    echo "  PASS: $desc"; PASS=$((PASS+1))
  else
    echo "  FAIL: $desc"; FAIL=$((FAIL+1))
  fi
}

SERVER_PID=""
wait_ready() {
  for _ in $(seq 1 50); do
    if curl -sf "$LOCK_BASE/healthz" >/dev/null && curl -sf "$RES_BASE/healthz" >/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  echo "server did not become ready" >&2; exit 1
}
start_server() {
  "$BIN" -lock-addr "$LOCK_ADDR" -resource-addr "$RES_ADDR" \
         -data-dir "$DATA_DIR" -ttl "${TTL_SECONDS}s" &
  SERVER_PID=$!
  wait_ready
}
stop_server() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
  fi
}
trap stop_server EXIT

post() { curl -sS -w '\n%{http_code}' -H 'Content-Type: application/json' -X POST "$1" -d "$2"; }
get()  { curl -sS -w '\n%{http_code}' "$1"; }
# split "<body>\n<status>" into BODY and STATUS
parse_resp() { BODY="${1%$'\n'*}"; STATUS="${1##*$'\n'}"; }
field() { jq -r "$1" <<<"$BODY"; }

echo "== Building =="
( cd "$ROOT_DIR" && go build -o "$BIN" ./cmd/fencingdemo )
echo "binary: $BIN"

echo
echo "== Starting services (lock $LOCK_ADDR, resource $RES_ADDR, TTL ${TTL_SECONDS}s) =="
rm -rf "$DATA_DIR"
start_server

echo
echo "== 1. Client A acquires the lock =="
parse_resp "$(post "$LOCK_BASE/lock/acquire" '{"lease_id":"A"}')"
echo "  HTTP $STATUS  $BODY"
TOKEN_A="$(field .fencing_token)"
check "A received fencing token 1 (got $TOKEN_A)" "$TOKEN_A" = 1 -a "$STATUS" = 200

echo
echo "== 2. A writes to the resource with token $TOKEN_A =="
parse_resp "$(post "$RES_BASE/resource/write" \
  "{\"fencing_token\":$TOKEN_A,\"lease_id\":\"A\",\"value\":\"write-by-A\"}")"
echo "  HTTP $STATUS  $BODY"
check "A's write accepted" "$STATUS" = 200 -a "$(field .accepted)" = true

echo
echo "== 3. A is paused; waiting $((TTL_SECONDS+1))s for its lease to expire =="
sleep $((TTL_SECONDS+1))
parse_resp "$(post "$LOCK_BASE/lock/renew" '{"lease_id":"A"}')"
echo "  A renew attempt after expiry: HTTP $STATUS  $BODY"
check "A's late renew rejected with 410 Gone" "$STATUS" = 410

echo
echo "== 4. Client B acquires the lock after A's lease expired =="
parse_resp "$(post "$LOCK_BASE/lock/acquire" '{"lease_id":"B"}')"
echo "  HTTP $STATUS  $BODY"
TOKEN_B="$(field .fencing_token)"
check "B received a strictly larger token ($TOKEN_B > $TOKEN_A)" \
      "$STATUS" = 200 -a "$TOKEN_B" -gt "$TOKEN_A"

echo
echo "== 5. B writes to the resource with token $TOKEN_B =="
parse_resp "$(post "$RES_BASE/resource/write" \
  "{\"fencing_token\":$TOKEN_B,\"lease_id\":\"B\",\"value\":\"write-by-B\"}")"
echo "  HTTP $STATUS  $BODY"
check "B's write accepted" "$STATUS" = 200 -a "$(field .accepted)" = true

echo
echo "== 6. A resumes and retries its write carrying STALE token $TOKEN_A =="
parse_resp "$(post "$RES_BASE/resource/write" \
  "{\"fencing_token\":$TOKEN_A,\"lease_id\":\"A\",\"value\":\"stale-write-by-A\"}")"
echo "  HTTP $STATUS  $BODY"
check "A's stale write rejected (409, accepted=false)" \
      "$STATUS" = 409 -a "$(field .accepted)" = false
check "response shows high-water mark $TOKEN_B" "$(field .high_water_mark)" = "$TOKEN_B"

parse_resp "$(get "$RES_BASE/resource/read")"
echo "  resource now: HTTP $STATUS  $BODY"
check "stored value is still B's, not A's" "$(field .value)" = "write-by-B"

echo
echo "== 7. Restarting both services (same data directory) =="
stop_server
start_server
echo "  services are back up"

parse_resp "$(get "$LOCK_BASE/lock/status")"
echo "  lock status: HTTP $STATUS  $BODY"
NEXT="$(field .next_token)"
check "lock next_token survived restart with no rollback (want $((TOKEN_B+1)), got $NEXT)" \
      "$NEXT" = "$((TOKEN_B+1))"

parse_resp "$(post "$RES_BASE/resource/write" \
  "{\"fencing_token\":$TOKEN_A,\"lease_id\":\"A\",\"value\":\"stale-after-restart\"}")"
echo "  A retries after restart: HTTP $STATUS  $BODY"
check "A's stale token still rejected after restart" "$STATUS" = 409

echo
echo "== 8. After B's lease expires, C acquires post-restart =="
sleep $((TTL_SECONDS+1))
parse_resp "$(post "$LOCK_BASE/lock/acquire" '{"lease_id":"C"}')"
echo "  HTTP $STATUS  $BODY"
TOKEN_C="$(field .fencing_token)"
check "C received token $((TOKEN_B+1)) with no rollback (got $TOKEN_C)" \
      "$STATUS" = 200 -a "$TOKEN_C" = "$((TOKEN_B+1))"

parse_resp "$(post "$RES_BASE/resource/write" \
  "{\"fencing_token\":$TOKEN_C,\"lease_id\":\"C\",\"value\":\"write-by-C\"}")"
echo "  HTTP $STATUS  $BODY"
check "C's write accepted" "$STATUS" = 200

parse_resp "$(get "$RES_BASE/resource/read")"
echo "  final resource: HTTP $STATUS  $BODY"
check "final value is C's" "$(field .value)" = "write-by-C"

echo
echo "== Summary: $PASS passed, $FAIL failed =="
[[ "$FAIL" == 0 ]]
