#!/usr/bin/env bash
# End-to-end demo over HTTP with windowSize=10, allowedLateness=2.
# Starts the server on PORT (default 18080), replays the deterministic
# acceptance sequence, prints each response, then stops the server.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-18091}"
BASE="http://127.0.0.1:$PORT"

# pretty-print JSON when python3 is available, otherwise print raw JSON
pp() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -m json.tool 2>/dev/null || cat
  else
    cat
  fi
}

step() {
  local title="$1"; shift
  echo
  echo "== $title"
  "$@" | pp
}

# like step, but for commands printing several JSON documents: no reformatting
stepraw() {
  local title="$1"; shift
  echo
  echo "== $title"
  "$@"
}

curlj() {
  curl -s -H 'Content-Type: application/json' "$@"
}

echo "starting server on port $PORT ..."
./run.sh --port "$PORT" --window-size 10 --allowed-lateness 2 > build/demo-server.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: server failed to start; log follows:" >&2
    cat build/demo-server.log >&2
    exit 1
  fi
  if curl -sf "$BASE/api/state" >/dev/null 2>&1; then break; fi
  sleep 0.2
done
echo "server is up."

step "1. four events into window [0,10) (incl. boundary ts=0)" \
  curlj -XPOST "$BASE/api/events" -d '[
    {"id":"e1","key":"a","eventTime":3,"partition":"p1"},
    {"id":"e2","key":"b","eventTime":7,"partition":"p2"},
    {"id":"e3","key":"a","eventTime":9,"partition":"p1"},
    {"id":"e10","key":"c","eventTime":0,"partition":"p2"}
  ]'

step "2. boundary events: ts=10 falls into [10,20), ts=19 stays there" \
  curlj -XPOST "$BASE/api/events" -d '[
    {"id":"e7","key":"a","eventTime":10,"partition":"p1"},
    {"id":"e8","key":"b","eventTime":19,"partition":"p2"}
  ]'

step "3. watermark p1=10: nothing closes yet (p2 at minimum)" \
  curlj -XPOST "$BASE/api/watermark" -d '{"partition":"p1","watermark":10}'

step "4. watermark p2=10: window [0,10) closes, count=4" \
  curlj -XPOST "$BASE/api/watermark" -d '{"partition":"p2","watermark":10}'

step "5. emitted results" curlj "$BASE/api/results"

step "6. e4 ts=5: late but within allowed lateness -> accepted" \
  curlj -XPOST "$BASE/api/events" -d '{"id":"e4","key":"a","eventTime":5,"partition":"p1"}'

step "7. e4 repeated -> duplicate, dropped" \
  curlj -XPOST "$BASE/api/events" -d '{"id":"e4","key":"a","eventTime":5,"partition":"p1"}'

step "8a. watermark p1=11 (p2 still 10): global unchanged" \
  curlj -XPOST "$BASE/api/watermark" -d '{"partition":"p1","watermark":11}'
step "8b. watermark p2=11: [0,10) re-fires, count=5" \
  curlj -XPOST "$BASE/api/watermark" -d '{"partition":"p2","watermark":11}'

stepraw "9. watermarks to 12: tolerance expired -> [0,10) purged" \
  bash -c "curl -s -H 'Content-Type: application/json' -XPOST '$BASE/api/watermark' -d '{\"partition\":\"p1\",\"watermark\":12}'; echo; curl -s -H 'Content-Type: application/json' -XPOST '$BASE/api/watermark' -d '{\"partition\":\"p2\",\"watermark\":12}'; echo"

step "10. e6 ts=6 after purge -> side output" \
  curlj -XPOST "$BASE/api/events" -d '{"id":"e6","key":"a","eventTime":6,"partition":"p1"}'
step "11. side output contents" curlj "$BASE/api/side-output"

stepraw "12. p1 marked idle; p2 jumps to 25: [10,20) closes AND purges" \
  bash -c "curl -s -H 'Content-Type: application/json' -XPOST '$BASE/api/partitions/idle' -d '{\"partition\":\"p1\"}'; echo; curl -s -H 'Content-Type: application/json' -XPOST '$BASE/api/watermark' -d '{\"partition\":\"p2\",\"watermark\":25}'; echo"

step "13. e11 ts=26 revives p1; global watermark stays 25" \
  curlj -XPOST "$BASE/api/events" -d '{"id":"e11","key":"a","eventTime":26,"partition":"p1"}'

stepraw "14. watermarks to 30: [20,30) closes with count=1" \
  bash -c "curl -s -H 'Content-Type: application/json' -XPOST '$BASE/api/watermark' -d '{\"partition\":\"p1\",\"watermark\":30}'; echo; curl -s -H 'Content-Type: application/json' -XPOST '$BASE/api/watermark' -d '{\"partition\":\"p2\",\"watermark\":30}'; echo"

step "15. final state snapshot" curlj "$BASE/api/state"

echo
echo "demo finished; stopping server."
