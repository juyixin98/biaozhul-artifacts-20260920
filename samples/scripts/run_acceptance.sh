#!/usr/bin/env bash
# End-to-end acceptance run for brpc-reuse.
#
#   1. cargo build (debug + release)
#   2. cargo test (unit + integration)
#   3. start demo-server on an ephemeral port
#   4. run the Rust demo-client (basic / concurrent / late / cancel)
#   5. run the independent Python frame tool: single requests, sticky+half
#      packet soak, and every fault injection
#   6. tear the server down
#
# Output is mirrored into test-results/ alongside a machine-readable status
# line. Exit non-zero if any step fails.
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."
mkdir -p test-results
LOG=test-results/acceptance.log
: > "$LOG"

PORT="${PORT:-$( (python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
) )}"
ADDR="127.0.0.1:${PORT}"
SERVER_PID=""
FAILED=0

log() { echo "$@" | tee -a "$LOG"; }
step() { log ""; log "===== $* ====="; }

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -INT "$SERVER_PID" 2>/dev/null
    sleep 0.3
    kill -9 "$SERVER_PID" 2>/dev/null
  fi
}
trap cleanup EXIT

step "1/6 cargo build (debug)"
cargo build 2>&1 | tee -a "$LOG" || FAILED=1

step "2/6 cargo build --release"
cargo build --release 2>&1 | tee -a "$LOG" || FAILED=1

step "3/6 cargo test"
cargo test -- --nocapture 2>&1 | tee -a "$LOG" || FAILED=1

step "4/6 start demo-server on $ADDR"
# max-inflight raised above the 100-request Python soak so that exercise
# measures multiplexing/framing, not the in-flight cap (the cap has its own
# dedicated integration tests).
./target/release/demo-server --bind "$ADDR" --max-inflight 256 >>"$LOG" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  if python3 -c "import socket,sys; sys.exit(0 if socket.create_connection(('127.0.0.1',${PORT}),0.2) else 1)" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
log "server pid $SERVER_PID"

step "5/6 Rust demo-client (all scenarios)"
./target/release/demo-client --addr "$ADDR" all 2>&1 | tee -a "$LOG" || FAILED=1

step "6/6 Python frame tool: interop + soak + fault injection"
python3 samples/scripts/frame_tool.py send "$ADDR" echo --id 901 --text 'python-echo' 2>&1 | tee -a "$LOG" || FAILED=1
python3 samples/scripts/frame_tool.py soak "$ADDR" --n 100 2>&1 | tee -a "$LOG" || FAILED=1
for kind in bad-crc bad-version oversized unknown-cmd bad-flags garbage half-frame dup-id cancel-unknown; do
  python3 samples/scripts/frame_tool.py inject "$ADDR" "$kind" 2>&1 | tee -a "$LOG" || FAILED=1
done

log ""
if [ "$FAILED" -eq 0 ]; then
  log "ACCEPTANCE RESULT: PASS (all steps succeeded)"
  echo PASS > test-results/status.txt
  exit 0
else
  log "ACCEPTANCE RESULT: FAIL (see $LOG)"
  echo FAIL > test-results/status.txt
  exit 1
fi
