#!/usr/bin/env bash
# One-shot verification: build, lint, test, and a live TCP smoke run.
# Records every command and result to docs/run_log.txt (stdout shown too).
set -u
cd "$(dirname "$0")/.."
LOG=docs/run_log.txt
: > "$LOG"

run() {
  echo "\$ $*" | tee -a "$LOG"
  "$@" 2>&1 | tee -a "$LOG"
  return "${PIPESTATUS[0]}"
}

echo "=== toolchain ===" | tee -a "$LOG"
run cargo --version
run rustc --version

echo "=== build (debug) ===" | tee -a "$LOG"
run cargo build
echo "=== build (release) ===" | tee -a "$LOG"
run cargo build --release

echo "=== clippy (all targets, -D warnings) ===" | tee -a "$LOG"
run cargo clippy --all-targets -- -D warnings

echo "=== tests ===" | tee -a "$LOG"
run cargo test

echo "=== live TCP smoke test (release binary) ===" | tee -a "$LOG"
PORT=16399
./target/release/resp-server --addr 127.0.0.1 --port "$PORT" \
  >/tmp/resp_smoke_server.log 2>&1 &
SPID=$!
sleep 0.3
trap 'kill $SPID 2>/dev/null' EXIT

ok=0; fail=0
check() { # name expected_bytes wire
  local name="$1" expected="$2" wire="$3"
  local got
  got=$(printf '%b' "$wire" | nc -q 1 127.0.0.1 "$PORT" | xxd -p | tr -d '\n')
  if [ "$got" = "$expected" ]; then
    echo "PASS $name" | tee -a "$LOG"; ok=$((ok+1))
  else
    echo "FAIL $name" | tee -a "$LOG"
    echo "  want: $expected" | tee -a "$LOG"
    echo "  got:  $got" | tee -a "$LOG"; fail=$((fail+1))
  fi
}

check ping        '2b504f4e470d0a' '*1\r\n$4\r\nPING\r\n'
check echo_crlf   '24360d0a61620d0a63640d0a' '*2\r\n$4\r\nECHO\r\n$6\r\nab\r\ncd\r\n'
check null_get    '242d310d0a' '*2\r\n$3\r\nGET\r\n$7\r\nmissing\r\n'

# empty-string value round trips as $0
printf '*3\r\n$3\r\nSET\r\n$2\r\nEK\r\n$0\r\n\r\n' | nc -q 1 127.0.0.1 "$PORT" >/dev/null
check empty_get   '24300d0a0d0a' '*2\r\n$3\r\nGET\r\n$2\r\nEK\r\n'

echo "smoke: $ok passed, $fail failed" | tee -a "$LOG"
[ "$fail" -eq 0 ]
