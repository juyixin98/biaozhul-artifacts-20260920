#!/usr/bin/env bash
# Restart + corruption E2E: verify the node reloads the last committed config,
# and reports (and recovers from) a tampered newest row.
set -o pipefail
WS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
source /opt/ros/jazzy/setup.bash
source "$WS_DIR/install/setup.bash"

NODE_BIN="$WS_DIR/install/robot_param_atomic/lib/robot_param_atomic/robot_config_node"
CLIENT_BIN="$WS_DIR/install/robot_param_atomic/lib/robot_param_atomic/robot_config_client"

DEMO_DIR="${1:-/tmp/robot_param_demo_restart}"
mkdir -p "$DEMO_DIR"
rm -f "$DEMO_DIR"/*
export ROBOT_PARAM_KEY=00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
DB="$DEMO_DIR/robot_param.sqlite3"

NODE_PID=""
start_node() {
  "$NODE_BIN" --ros-args -r __node:=robot_config -p "db_path:=$DB" \
    > "$DEMO_DIR/node.$1.log" 2>&1 &
  NODE_PID=$!
  sleep 3
  kill -0 "$NODE_PID" 2>/dev/null || { echo "node $1 failed to start"; cat "$DEMO_DIR/node.$1.log"; exit 1; }
}
stop_node() {
  [ -n "$NODE_PID" ] || return 0
  kill "$NODE_PID" 2>/dev/null || true
  for _ in $(seq 1 50); do
    kill -0 "$NODE_PID" 2>/dev/null || break
    sleep 0.1
  done
  kill -9 "$NODE_PID" 2>/dev/null || true
  wait "$NODE_PID" 2>/dev/null || true
  NODE_PID=""
}

# sql <STATEMENT>: run SQL via Python's stdlib sqlite3 (no sqlite3 CLI needed).
sql() {
  python3 - "$DB" "$1" <<'PY'
import sqlite3, sys
db, stmt = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.execute(stmt)
con.commit()
PY
}

fail=0
expect() { # desc, pattern, text
  if echo "$3" | grep -q "$2"; then echo "OK:   $1";
  else echo "FAIL: $1 (wanted pattern: $2)"; fail=1; fi
}

# --- Phase 1: two commits, then stop ---
start_node 1
"$CLIENT_BIN" update --expected-version 0 --buffer 4000 > /dev/null || fail=1
"$CLIENT_BIN" update --expected-version 1 --rate 125 > /dev/null || fail=1
echo "rows on disk:"
python3 - "$DB" <<'PY'
import sqlite3, sys
for row in sqlite3.connect(sys.argv[1]).execute(
    "SELECT version, sample_rate_hz, buffer_length, allowed_latency_ms FROM config_versions"):
  print(row)
PY
stop_node

# --- Phase 2: clean restart must load v2 ---
start_node 2
OUT=$("$CLIENT_BIN" get)
echo "$OUT"
expect "version 2 after restart" '"version": 2' "$OUT"
expect "rate 125 restored"       '"sample_rate_hz": 125' "$OUT"
expect "load_state LOADED"       '"load_state": "LOADED"' "$OUT"
stop_node

# --- Phase 3: tamper v2 payload without touching hashes, restart -> RECOVERED v1 ---
sql "UPDATE config_versions SET allowed_latency_ms = 4242 WHERE version = 2"
start_node 3
OUT=$("$CLIENT_BIN" get)
echo "$OUT"
expect "fallback to version 1"   '"version": 1' "$OUT"
expect "load_state RECOVERED"    '"load_state": "RECOVERED"' "$OUT"
expect "SHA-256 mismatch report" "SHA-256 mismatch" "$OUT"
expect "WARN fallback log"       "fell back to version 1" "$(cat "$DEMO_DIR/node.3.log")"

# New commit chains on as v3 (max existing version + 1)
R=$("$CLIENT_BIN" update --expected-version 1 --latency 40)
echo "$R"
expect "next commit is version 3" '"version": 3' "$R"
stop_node

# --- Phase 4: foreign/rotated key -> NO_VALID_CONFIG ---
# Remove the key file and start WITHOUT ROBOT_PARAM_KEY so the node mints a
# fresh random key; every existing row must then fail HMAC verification.
rm -f "$DB.key"
env -u ROBOT_PARAM_KEY "$NODE_BIN" --ros-args -r __node:=robot_config_foreign \
  -p "db_path:=$DB" > "$DEMO_DIR/node.4.log" 2>&1 &
NODE_PID=$!
sleep 3
kill -0 "$NODE_PID" 2>/dev/null || { echo "node 4 failed"; cat "$DEMO_DIR/node.4.log"; exit 1; }
OUT=$("$CLIENT_BIN" --node /robot_config_foreign get)
echo "$OUT"
expect "NO_VALID_CONFIG under new key" '"load_state": "NO_VALID_CONFIG"' "$OUT"
expect "HMAC mismatch report"          "HMAC mismatch" "$OUT"
stop_node

echo
if [ "$fail" -ne 0 ]; then echo "RESTART E2E FAILED"; exit 1; fi
echo "RESTART/CORRUPTION E2E PASSED"
exit 0
