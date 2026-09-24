#!/usr/bin/env bash
# End-to-end acceptance for the watchdog recovery state machine.
#
# Starts the real Axum server against a temp SQLite database with a
# deterministic clock, then proves over HTTP:
#   1. feeding needs EVERY task to advance within the window
#   2. repeated identical heartbeats are NOT progress
#   3. a stuck task accumulates consecutive resets and reaches safe mode
#   4. ordinary heartbeats can never clear safe mode
#   5. stale (old-generation) clearances are rejected
#   6. bad HMAC is rejected; valid HMAC for the current fault generation works
#   7. the host process can restart and all counters survive
#
# HMAC is computed independently here with python3 and cross-checked against
# the Rust sign-clearance tool.
set -uo pipefail

BASE="${BASE:-http://127.0.0.1:18078}"
KEY="dev-operator-key-change-me"
DBDIR="$(mktemp -d)"
DB="$DBDIR/wd.db"
DEV="sensor-node-07"
SRV_PID=""
FAIL=0

trap 'if [ -n "$SRV_PID" ]; then kill "$SRV_PID" 2>/dev/null || true; fi; rm -rf "$DBDIR"' EXIT

say()  { printf '\n=== %s ===\n' "$*"; }
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; FAIL=1; }
check() { # desc actual expected
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (got '$2', want '$3')"; fi
}

start_server() {
    WATCHDOG_MANUAL_CLOCK=1 WATCHDOG_DB="$DB" WATCHDOG_ADDR="${BASE#http://}" \
        cargo run --quiet --bin watchdog-host >"$DBDIR/server.log" 2>&1 &
    SRV_PID=$!
    for _ in $(seq 1 120); do
        if curl -sf "$BASE/health" >/dev/null 2>&1; then return 0; fi
        kill -0 "$SRV_PID" 2>/dev/null || { echo "server died:"; cat "$DBDIR/server.log"; exit 1; }
        sleep 0.5
    done
    echo "server did not become healthy:"; cat "$DBDIR/server.log"; exit 1
}
stop_server() { kill "$SRV_PID" 2>/dev/null || true; wait "$SRV_PID" 2>/dev/null || true; SRV_PID=""; }

BODY_FILE="$DBDIR/body.json"
HTTP=0; BODY=""
api() { # METHOD PATH [JSON-DATA]
    if [ "${3:-}" = "" ]; then
        HTTP=$(curl -s -o "$BODY_FILE" -w '%{http_code}' -X "$1" -H 'content-type: application/json' "$BASE$2")
    else
        HTTP=$(curl -s -o "$BODY_FILE" -w '%{http_code}' -X "$1" -H 'content-type: application/json' --data "$3" "$BASE$2")
    fi
    BODY=$(cat "$BODY_FILE")
}
jqr() { printf '%s' "$BODY" | jq -r "$1"; }

hmac_python() { # generation challenge
    python3 - "$KEY" "$1" "$2" <<'PY'
import hmac, hashlib, sys
key, generation, challenge = sys.argv[1], sys.argv[2], sys.argv[3]
print(hmac.new(key.encode(), f"{generation}:{challenge}".encode(), hashlib.sha256).hexdigest())
PY
}

say "build"
cargo build --quiet --bins || { echo "build failed"; exit 1; }

say "start server (manual clock, fresh db: $DB)"
start_server

say "1) create device + two critical tasks"
api POST /devices "{\"device_id\":\"$DEV\",\"window_ms\":1000,\"reset_threshold\":3,\"operator_key\":\"$KEY\"}"
check "create device -> 201" "$HTTP" "201"
check "initial status running" "$(jqr .status)" "running"
api POST "/devices/$DEV/tasks" '{"task_id":"net-loop","initial_counter":0}'
check "register net-loop -> 200" "$HTTP" "200"
api POST "/devices/$DEV/tasks" '{"task_id":"storage-flush","initial_counter":0}'
check "register storage-flush -> 200" "$HTTP" "200"

say "2) full progress feeds; partial and repeated heartbeats do not"
api POST "/devices/$DEV/heartbeat" '{"reports":[{"task_id":"net-loop","counter":1}]}'
check "single-task heartbeat not fed" "$(jqr .fed)" "false"
api POST "/devices/$DEV/heartbeat" '{"reports":[{"task_id":"net-loop","counter":1},{"task_id":"storage-flush","counter":1}]}'
check "both advanced -> fed" "$(jqr .fed)" "true"
api POST "/devices/$DEV/heartbeat" '{"reports":[{"task_id":"net-loop","counter":1},{"task_id":"storage-flush","counter":1}]}'
check "identical counters repeated -> not fed" "$(jqr .fed)" "false"

say "3) stuck task: three missed windows -> safe mode, generation 1"
for n in 1 2 3; do
    api POST /admin/clock/advance '{"ms":1000}'
    api POST "/devices/$DEV/tick"
    check "tick $n applied a reset" "$(jqr .reset_applied)" "true"
done
check "consecutive resets = 3" "$(jqr .consecutive_resets)" "3"
check "status safe_mode" "$(jqr .status)" "safe_mode"
check "fault generation = 1" "$(jqr .fault_generation)" "1"

say "4) heartbeats CANNOT clear safe mode (the core safety property)"
for n in 10 11 12; do
    api POST "/devices/$DEV/heartbeat" \
        "{\"reports\":[{\"task_id\":\"net-loop\",\"counter\":$n},{\"task_id\":\"storage-flush\",\"counter\":$n}]}"
    check "heartbeat in safe mode not fed" "$(jqr .fed)" "false"
    check "heartbeat reports safe_mode" "$(jqr .safe_mode)" "true"
done
api GET "/devices/$DEV"
check "device still safe_mode after heartbeats" "$(jqr .status)" "safe_mode"

say "5) stale clearance (old generation) rejected"
api POST "/devices/$DEV/safe-mode/challenge"
NONCE1="$(jqr .challenge)"
check "challenge binds generation 1" "$(jqr .generation)" "1"
OLD_SIG="$(hmac_python 0 "$NONCE1")"
api POST "/devices/$DEV/safe-mode/clear" \
    "{\"generation\":0,\"challenge\":\"$NONCE1\",\"hmac_hex\":\"$OLD_SIG\"}"
check "generation 0 clearance -> 403" "$HTTP" "403"
if printf '%s' "$BODY" | grep -q stale; then pass "rejection reason mentions stale generation"; else fail "expected 'stale' in $BODY"; fi

say "6) wrong HMAC rejected; independent HMAC implementations agree"
BAD_SIG="$(hmac_python 1 "$NONCE1" | sed 's/^./X/')"
api POST "/devices/$DEV/safe-mode/clear" \
    "{\"generation\":1,\"challenge\":\"$NONCE1\",\"hmac_hex\":\"$BAD_SIG\"}"
check "tampered HMAC -> 403" "$HTTP" "403"

SIG_PY="$(hmac_python 1 "$NONCE1")"
SIG_RS="$(cargo run --quiet --bin sign-clearance -- --key "$KEY" --generation 1 --challenge "$NONCE1")"
check "python3 HMAC == Rust HMAC tool" "$SIG_PY" "$SIG_RS"

api POST "/devices/$DEV/safe-mode/clear" \
    "{\"generation\":1,\"challenge\":\"$NONCE1\",\"hmac_hex\":\"$SIG_PY\"}"
check "valid current-generation clearance -> 200" "$HTTP" "200"
check "cleared flag" "$(jqr .cleared)" "true"
api GET "/devices/$DEV"
check "status back to running" "$(jqr .status)" "running"
check "streak reset to zero" "$(jqr .consecutive_resets)" "0"

say "7) new fault invalidates the old clearance token"
for _ in 1 2 3; do
    api POST /admin/clock/advance '{"ms":1000}'
    api POST "/devices/$DEV/tick" >/dev/null
done
api GET "/devices/$DEV"
check "re-entered safe_mode" "$(jqr .status)" "safe_mode"
check "fault generation bumped to 2" "$(jqr .fault_generation)" "2"
api POST "/devices/$DEV/safe-mode/clear" \
    "{\"generation\":1,\"challenge\":\"$NONCE1\",\"hmac_hex\":\"$SIG_PY\"}"
check "old (gen-1) token after new fault -> 403" "$HTTP" "403"
# recover so the device is running before the restart check
api POST "/devices/$DEV/safe-mode/challenge"
NONCE2="$(jqr .challenge)"
SIG2="$(hmac_python 2 "$NONCE2")"
api POST "/devices/$DEV/safe-mode/clear" \
    "{\"generation\":2,\"challenge\":\"$NONCE2\",\"hmac_hex\":\"$SIG2\"}"
check "gen-2 clearance -> 200" "$HTTP" "200"

say "8) firmware reports a reset carrying the persistent boot count"
api POST "/devices/$DEV/reset" \
    '{"reason":"brownout during flash erase","source":"brownout_detector","boot_count":7}'
check "firmware reset accepted -> 200" "$HTTP" "200"
check "boot count stored" "$(jqr .boot_count)" "7"

say "9) restart host process: all counters survive (SQLite persistence)"
stop_server
start_server
api GET "/devices/$DEV"
check "status survived restart" "$(jqr .status)" "running"
check "boot count survived restart" "$(jqr .boot_count)" "7"
check "fault generation survived restart" "$(jqr .fault_generation)" "2"
check "last progress counters retained" "$(jqr '.last_progress["net-loop"]')" "1"
api GET "/devices/$DEV/resets"
check "reset history persisted (6 watchdog + 1 brownout)" "$(jqr '.resets|length')" "7"
check "brownout reason retained" "$(jqr '.resets[6].reason')" "brownout during flash erase"

say "reset record contents (forensics)"
printf '%s\n' "$BODY" | jq '.resets[] | {seq, source, reason, consecutive_after, fault_generation_after}'

stop_server

echo
if [ "$FAIL" -eq 0 ]; then
    echo "ALL ACCEPTANCE CHECKS PASSED"
else
    echo "ACCEPTANCE FAILED (see FAIL lines above)"
fi
exit "$FAIL"
