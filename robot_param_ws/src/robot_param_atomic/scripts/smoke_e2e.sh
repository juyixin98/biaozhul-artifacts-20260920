#!/usr/bin/env bash
# End-to-end smoke test using two real processes (node + CLI client).
set -o pipefail
WS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
source /opt/ros/jazzy/setup.bash
source "$WS_DIR/install/setup.bash"

NODE_BIN="$WS_DIR/install/robot_param_atomic/lib/robot_param_atomic/robot_config_node"
CLIENT_BIN="$WS_DIR/install/robot_param_atomic/lib/robot_param_atomic/robot_config_client"

DEMO_DIR="${1:-/tmp/robot_param_demo}"
mkdir -p "$DEMO_DIR"
rm -f "$DEMO_DIR"/*

# Fixed 32-byte key (64 hex) so restarts reuse the same HMAC secret.
export ROBOT_PARAM_KEY=00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
DB="$DEMO_DIR/robot_param.sqlite3"

"$NODE_BIN" --ros-args -r __node:=robot_config -p "db_path:=$DB" \
  > "$DEMO_DIR/node.log" 2>&1 &
NODE_PID=$!

cleanup() {
  kill "$NODE_PID" 2>/dev/null || true
  for _ in $(seq 1 50); do kill -0 "$NODE_PID" 2>/dev/null || break; sleep 0.1; done
  kill -9 "$NODE_PID" 2>/dev/null || true
  wait "$NODE_PID" 2>/dev/null || true
}
trap cleanup EXIT

sleep 3
if ! kill -0 "$NODE_PID" 2>/dev/null; then
  echo "NODE FAILED TO START"; cat "$DEMO_DIR/node.log"; exit 1
fi

fail=0
check() { # desc, expected_exit, actual_exit
  if [ "$2" != "$3" ]; then echo "FAIL: $1 (expected exit $2, got $3)"; fail=1;
  else echo "OK:   $1"; fi
}

echo "=== 1. get (fresh defaults) ==="
"$CLIENT_BIN" get
check "get defaults" 0 $?

echo "=== 2. legal batch @v0: buffer=5000 ==="
"$CLIENT_BIN" update --expected-version 0 --buffer 5000
check "legal commit @v0" 0 $?

echo "=== 3. each field legal, combination illegal @v1: latency=60000 ==="
"$CLIENT_BIN" update --expected-version 1 --latency 60000
check "cross-field rejected" 1 $?

echo "=== 4. stale expected_version=0 ==="
"$CLIENT_BIN" update --expected-version 0 --rate 200
check "version conflict" 2 $?

echo "=== 5. legal @v1: rate=200 (covers 25000ms >= 2*50) ==="
"$CLIENT_BIN" update --expected-version 1 --rate 200
check "legal commit @v1" 0 $?

echo "=== 6. live snapshot topic for a fresh commit (volatile subscription) ==="
( timeout 8 ros2 topic echo --once --qos-durability volatile \
    /robot_config/snapshots > "$DEMO_DIR/echo.out" 2>/dev/null ) &
ECHO_PID=$!
sleep 2
"$CLIENT_BIN" update --expected-version 2 --latency 45 > /dev/null
wait "$ECHO_PID" 2>/dev/null || true
cat "$DEMO_DIR/echo.out"
grep -q "version: 3" "$DEMO_DIR/echo.out" && grep -q "allowed_latency_ms: 45" "$DEMO_DIR/echo.out" \
  && echo "OK:   snapshot v3 published on topic" \
  || { echo "FAIL: live snapshot"; fail=1; }

echo "=== node log ==="
cat "$DEMO_DIR/node.log"

echo
if [ "$fail" -ne 0 ]; then echo "SMOKE TEST FAILED"; exit 1; fi
echo "ALL SMOKE CHECKS PASSED"
exit 0
