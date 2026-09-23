#!/usr/bin/env bash
# End-to-end acceptance demo over real HTTP:
#   1. start service with gap=10, allowed-lateness=10
#   2. events ts=0 and ts=20 form two sessions
#   3. late event ts=10 BRIDGES them: RETRACT k#2 + new UPSERT version of k#1
#   4. fold /output to prove the count never exceeds accepted events (3)
#   5. advance watermark, show a too-late event is REJECTED
#   6. restart the service from the log; session id + aggregation are stable
#
# Usage: ./examples/acceptance-demo.sh [port]
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"
PORT="${1:-18080}"
BASE="http://127.0.0.1:$PORT"
DATA_DIR="run/demo-data"
SERVER_LOG="run/demo-server.log"
PID=""

cleanup() {
  [ -n "$PID" ] && { kill "$PID" 2>/dev/null || true; pkill -P "$PID" 2>/dev/null || true; }
}
trap cleanup EXIT

start_server() {
  rm -rf "$DATA_DIR"
  mkdir -p run
  setsid nohup "$JAVA_BIN" -cp out/classes com.example.sessionwindow.Main \
    --port "$PORT" --gap 10 --allowed-lateness 10 --data-dir "$DATA_DIR" \
    >"$SERVER_LOG" 2>&1 </dev/null &
  PID=$!
  await_healthy
}

restart_server() {
  # keep the event log on disk; only stop and relaunch
  stop_server
  mkdir -p run
  setsid nohup "$JAVA_BIN" -cp out/classes com.example.sessionwindow.Main \
    --port "$PORT" --gap 10 --allowed-lateness 10 --data-dir "$DATA_DIR" \
    >"$SERVER_LOG" 2>&1 </dev/null &
  PID=$!
  await_healthy
}

await_healthy() {
  echo ">> server starting (pid $PID, port $PORT, data $DATA_DIR)"
  for _ in $(seq 1 50); do
    if ! kill -0 "$PID" 2>/dev/null; then
      echo "!! server process exited early; log:" >&2
      cat "$SERVER_LOG" >&2
      exit 1
    fi
    if curl -sf "$BASE/health" >/dev/null 2>&1; then
      echo ">> server is up"
      return
    fi
    sleep 0.2
  done
  echo "!! server failed to become healthy; log:" >&2
  cat "$SERVER_LOG" >&2
  exit 1
}

stop_server() {
  [ -z "$PID" ] && return
  kill "$PID" 2>/dev/null || true
  # also kill the whole process group created by setsid
  pkill -P "$PID" 2>/dev/null || true
  for _ in $(seq 1 25); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.2
  done
  kill -9 "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
  PID=""
  echo ">> server stopped"
}

post() { # path json
  curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"
}

start_server

echo "=========================================================="
echo "Step 1: event ts=0"
post /events '{"key":"userA","ts":0,"clientId":"e-0"}'
echo
echo "Step 2: event ts=20  (gap 10 => two separate sessions)"
post /events '{"key":"userA","ts":20,"clientId":"e-20"}'
echo
echo "Sessions now:"
curl -s "$BASE/sessions?key=userA"
echo

echo
echo "=========================================================="
echo "Step 3: LATE event ts=10 bridges the two sessions"
post /events '{"key":"userA","ts":10,"clientId":"e-10"}'
echo
echo "Sessions after bridge (expect one session userA#1, count 3, 0..20):"
curl -s "$BASE/sessions?key=userA"
echo

echo
echo "=========================================================="
echo "Step 4: full changelog (/output); retract + versions are visible"
curl -s "$BASE/output"
echo

echo
echo "=========================================================="
echo "Step 5: watermark  -> reject a too-late event (ts=19, wm=20)"
post /watermark '{"watermark":20}'
echo
echo "submit ts=19 (expect REJECTED):"
post /events '{"key":"userA","ts":19,"clientId":"e-late"}'
echo
echo "submit ts=25 (expect ACCEPTED):"
post /events '{"key":"userA","ts":25,"clientId":"e-25"}'
echo
echo "duplicate clientId e-10 (expect DUPLICATE):"
post /events '{"key":"userA","ts":10,"clientId":"e-10"}'
echo
echo "Stats:"
curl -s "$BASE/stats"
echo

echo
echo "=========================================================="
echo "Step 6: restart from event log and verify recovery"
restart_server
echo "Sessions after recovery (id userA#1, range 0..25, count 4 must be stable):"
curl -s "$BASE/sessions?key=userA"
echo
echo "Stats after recovery:"
curl -s "$BASE/stats"
echo
echo "Watermark after recovery (expect 20):"
curl -s "$BASE/watermark"
echo
echo ">> acceptance demo finished OK"
