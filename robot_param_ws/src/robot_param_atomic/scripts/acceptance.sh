#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# End-to-end acceptance test for robot_param_atomic.
#
# It builds (optional), starts a real node, drives it with the real parameter
# client over rclcpp services, and asserts every required behaviour:
#   * valid atomic commit
#   * single-field legal but cross-field combination illegal  -> code 1
#   * stale expected_version (optimistic concurrency)          -> code 2
#   * commit attempted from inside a change callback           -> code 3
#   * persistence failure (injected) rolls back                -> code 4
#   * N concurrent CAS committers: exactly one wins
#   * restart loads the latest committed configuration
#   * corrupt newest record rejected with an explicit status, falls back
#
# Usage:
#   ./acceptance.sh            # assumes the workspace is already built+sourced
#   ./acceptance.sh --build    # colcon build + source first
#
# Exit code: number of failed assertions (0 = everything passed).
# NOTE: `set -u` is enabled only AFTER sourcing ROS setup files, because those
# scripts reference possibly-unset variables (e.g. AMENT_TRACE_SETUP_FILES).
set -o pipefail

BUILD_FIRST=0
[ "${1:-}" = "--build" ] && BUILD_FIRST=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WS_DIR="$(cd "$PKG_DIR/../.." && pwd)"

# Isolated DDS domain so the run never disturbs other graphs on the machine.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-67}"
WORKDIR="$(mktemp -d /tmp/robot_param_accept.XXXXXX)"
DB="$WORKDIR/accept.db"
NODE_LOG="$WORKDIR/node.log"
NODE_PID=""
PASS=0
FAIL=0

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '\033[36m[run]\033[0m %s\n' "$*"; }

check() {
  # check "<description>" <expected_exit> <actual_exit> [grep_pattern] [haystack]
  local desc="$1" want="$2" got="$3" pat="${4:-}" hay="${5:-}"
  if [ "$want" = "$got" ]; then
    if [ -n "$pat" ] && ! printf '%s' "$hay" | grep -q "$pat"; then
      red "FAIL: $desc (exit matched, but missing /$pat/)"
      FAIL=$((FAIL+1))
    else
      grn "PASS: $desc"
      PASS=$((PASS+1))
    fi
  else
    red "FAIL: $desc (expected exit $want, got $got)"
    [ -n "$hay" ] && printf '   output: %s\n' "$hay"
    FAIL=$((FAIL+1))
  fi
}

cleanup() {
  [ -n "$NODE_PID" ] && kill "$NODE_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

# ---- environment -----------------------------------------------------------
# shellcheck disable=SC1091
source /opt/ros/jazzy/setup.bash
if [ "$BUILD_FIRST" = "1" ]; then
  info "building workspace"
  ( cd "$WS_DIR" && colcon build --packages-select robot_param_atomic \
      --cmake-args -DBUILD_TESTING=ON ) || { red "build failed"; exit 1; }
fi
# shellcheck disable=SC1091
source "$WS_DIR/install/setup.bash"
set -u

NODE_BIN="$(ros2 pkg prefix robot_param_atomic)/lib/robot_param_atomic/robot_config_node"
CLIENT_BIN="$(ros2 pkg prefix robot_param_atomic)/lib/robot_param_atomic/robot_config_client"
[ -x "$NODE_BIN" ] || { red "node binary missing: $NODE_BIN (run with --build)"; exit 1; }
[ -x "$CLIENT_BIN" ] || { red "client binary missing: $CLIENT_BIN"; exit 1; }

start_node() {
  # start_node <db> [extra ros args...]
  # Set KEEP_DB=1 to (re)open an EXISTING database without deleting it, as in
  # restart / corrupt-record scenarios.
  local db="$1"; shift
  if [ "${KEEP_DB:-0}" != "1" ]; then
    rm -f "$db" "$db-wal" "$db-shm"
  fi
  "$NODE_BIN" --ros-args -p "db_path:=$db" "$@" > "$NODE_LOG" 2>&1 &
  NODE_PID=$!
  for _ in $(seq 1 40); do
    "$CLIENT_BIN" get >/dev/null 2>&1 && return 0
    kill -0 "$NODE_PID" 2>/dev/null || { red "node died early"; cat "$NODE_LOG"; return 1; }
    sleep 0.25
  done
  red "node did not become ready"; cat "$NODE_LOG"; return 1
}
stop_node() { kill "$NODE_PID" 2>/dev/null; wait "$NODE_PID" 2>/dev/null; NODE_PID=""; }

# client wrapper echoes "<output>\n__EXIT__<code>"
run_set()   { out=$("$CLIENT_BIN" set   "$@" 2>/dev/null); rc=$?; printf '%s\n__EXIT__%s' "$out" "$rc"; }
run_patch() { out=$("$CLIENT_BIN" patch "$@" 2>/dev/null); rc=$?; printf '%s\n__EXIT__%s' "$out" "$rc"; }
field() { printf '%s' "$1" | sed -n '1p'; }
rcc()   { printf '%s' "$1" | sed -n '2p' | sed 's/__EXIT__//'; }

# ---------------------------------------------------------------------------
info "workspace: $WS_DIR"
info "workdir:   $WORKDIR  (ROS_DOMAIN_ID=$ROS_DOMAIN_ID)"
start_node "$DB"

# 1) defaults
info "read defaults"
out=$("$CLIENT_BIN" get); rc=$?
check "get defaults reachable" 0 "$rc"
printf '%s' "$out" | grep -q '"version":0' && { grn "PASS: initial version 0"; PASS=$((PASS+1)); } || { red "FAIL: version 0"; FAIL=$((FAIL+1)); }

# 2) valid commit
info "valid atomic commit"
res=$(run_set --expected 0 --rate 200 --cache 2.0 --latency 0.5)
check "valid commit accepted" 0 "$(rcc "$res")" '"ok":true' "$(field "$res")"

# 3) cross-field illegal combination
info "single fields legal, combination illegal (cache 0.4 < 2*0.3)"
res=$(run_set --expected 1 --rate 1000 --cache 0.4 --latency 0.3)
check "illegal combination rejected" 1 "$(rcc "$res")" '"code":1' "$(field "$res")"

# 4) stale version
info "stale expected_version"
res=$(run_set --expected 0 --rate 300 --cache 4.0 --latency 1.0)
check "stale version rejected" 1 "$(rcc "$res")" '"code":2' "$(field "$res")"

# 5) correct token commits
res=$(run_set --expected 1 --rate 300 --cache 4.0 --latency 1.0)
check "fresh version accepted -> v2" 0 "$(rcc "$res")" '"version":2' "$(field "$res")"

# 6) partial patch breaking invariant
info "partial patch that breaks invariant"
res=$(run_patch --expected 2 --latency 2.5)   # needs cache>=5, cache=4
check "breaking patch rejected" 1 "$(rcc "$res")" '"code":1' "$(field "$res")"

# 7) concurrency
info "10 concurrent CAS commits on the same token (exactly one must win)"
out=$("$CLIENT_BIN" race --expected 2 --threads 10 --rate 500 --cache 6 --latency 2)
rc=$?
check "race: exactly 1 win / 9 stale (client self-check exit 0)" 0 "$rc"
wins=$(printf '%s' "$out" | sed -n 's/.*"wins":\([0-9]*\).*/\1/p')
stale=$(printf '%s' "$out" | sed -n 's/.*"stale_rejections":\([0-9]*\).*/\1/p')
[ "$wins" = "1" ] && [ "$stale" = "9" ] \
  && { grn "PASS: concurrency wins=1 stale=9"; PASS=$((PASS+1)); } \
  || { red "FAIL: concurrency wins=$wins stale=$stale"; FAIL=$((FAIL+1)); }

stop_node

# 8) restart persistence
info "restart loads latest committed config"
KEEP_DB=1 start_node "$DB"
out=$("$CLIENT_BIN" get); rc=$?
check "get after restart" 0 "$rc"
printf '%s' "$out" | grep -q '"version":3' && printf '%s' "$out" | grep -q '"sampling_rate_hz":500' \
  && { grn "PASS: latest v3 snapshot restored"; PASS=$((PASS+1)); } \
  || { red "FAIL: restart snapshot"; printf '   %s\n' "$out"; FAIL=$((FAIL+1)); }
grep -q '\[load\] LOADED' "$NODE_LOG" \
  && { grn "PASS: node reported LOADED status"; PASS=$((PASS+1)); } \
  || { red "FAIL: LOADED status not logged"; FAIL=$((FAIL+1)); }

# 9) callback re-entrancy (probe node)
info "commit from inside change callback must be rejected (code 3)"
stop_node
start_node "$WORKDIR/probe.db" -p probe_callback_commit:=true
res=$(run_set --expected 0 --rate 50 --cache 2 --latency 0.5)
check "outer commit succeeds" 0 "$(rcc "$res")"
sleep 0.5
grep -q 'REJECT_UPDATE_IN_CALLBACK' "$NODE_LOG" \
  && { grn "PASS: nested callback commit rejected without deadlock"; PASS=$((PASS+1)); } \
  || { red "FAIL: callback re-entrancy"; FAIL=$((FAIL+1)); }
stop_node

# 10) persistence failure injection
info "persistence failure must roll back (PARAM_ATOMIC_FAULT=commit_io)"
rm -f "$WORKDIR/fault.db"
PARAM_ATOMIC_FAULT=commit_io "$NODE_BIN" --ros-args -p "db_path:=$WORKDIR/fault.db" > "$NODE_LOG" 2>&1 &
NODE_PID=$!
for _ in $(seq 1 40); do "$CLIENT_BIN" get >/dev/null 2>&1 && break; sleep 0.25; done
res=$(run_set --expected 0 --rate 200 --cache 2 --latency 0.5)
check "persistence failure -> code 4" 1 "$(rcc "$res")" '"code":4' "$(field "$res")"
out=$("$CLIENT_BIN" get)
printf '%s' "$out" | grep -q '"version":0' \
  && { grn "PASS: state unchanged at v0 after failed persistence"; PASS=$((PASS+1)); } \
  || { red "FAIL: state changed after persistence failure"; FAIL=$((FAIL+1)); }
nrows=$(python3 -c "import sqlite3;print(sqlite3.connect('$WORKDIR/fault.db').execute('select count(*) from config_commits').fetchone()[0])")
[ "$nrows" = "0" ] && { grn "PASS: no rows persisted on rollback"; PASS=$((PASS+1)); } \
  || { red "FAIL: $nrows rows persisted despite rollback"; FAIL=$((FAIL+1)); }
stop_node

# 11) corrupt newest record
info "corrupt newest record is rejected, falls back to previous good"
cp "$DB" "$WORKDIR/corrupt.db"
python3 -c "
import sqlite3
c=sqlite3.connect('$WORKDIR/corrupt.db')
c.execute('UPDATE config_commits SET sampling_rate_hz=9999 WHERE id=(SELECT max(id) FROM config_commits)')
c.commit()"
KEEP_DB=1 start_node "$WORKDIR/corrupt.db"
out=$("$CLIENT_BIN" get)
printf '%s' "$out" | grep -q '"version":2' \
  && { grn "PASS: fell back to latest good v2"; PASS=$((PASS+1)); } \
  || { red "FAIL: fallback snapshot wrong: $out"; FAIL=$((FAIL+1)); }
grep -q '\[load\] CORRUPT_REJECTED' "$NODE_LOG" && grep -q 'SHA-256 mismatch' "$NODE_LOG" \
  && { grn "PASS: explicit CORRUPT_REJECTED status with hash detail"; PASS=$((PASS+1)); } \
  || { red "FAIL: corrupt status not logged"; FAIL=$((FAIL+1)); }
stop_node

# ---- summary ---------------------------------------------------------------
echo
echo "================ acceptance summary ================"
echo "passed: $PASS   failed: $FAIL"
echo "artifacts kept in: $WORKDIR"
[ "$FAIL" = "0" ] && grn "ACCEPTANCE PASSED" || red "ACCEPTANCE FAILED"
exit "$FAIL"
