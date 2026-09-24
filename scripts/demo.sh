#!/usr/bin/env bash
# End-to-end demo of the gang scheduler against a real running server.
# Starts gangd, runs the acceptance scenario (two gangs competing for
# overlapping nodes; node goes OFFLINE between reserve and commit), and
# records every request/response to docs/demo-output.log.
set -u
set -o pipefail

cd "$(dirname "$0")/.."
# pick a free loopback port unless one is forced
pick_port() {
  if command -v python3 >/dev/null; then
    python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'
  else
    echo $(( ( RANDOM % 20000 ) + 20000 ))
  fi
}
PORT="${PORT:-$(pick_port)}"
BASE="http://127.0.0.1:${PORT}"
LOG="docs/demo-output.log"
mkdir -p docs
: > "$LOG"

# find go
GO="$(command -v go || echo /usr/local/go/bin/go)"

echo "# building gangd..."
"$GO" build -o /tmp/gangd-demo ./cmd/gangd

echo "# starting gangd on :${PORT} (ttl=5s)..."
/tmp/gangd-demo -addr ":${PORT}" -ttl 5s -reap 50ms >/tmp/gangd-demo.stderr 2>&1 &
SRV_PID=$!
trap 'kill ${SRV_PID} 2>/dev/null || true' EXIT

READY=0
for _ in $(seq 1 50); do
  if ! kill -0 "$SRV_PID" 2>/dev/null; then
    echo "server failed to start:"; cat /tmp/gangd-demo.stderr; exit 1
  fi
  if curl -sf "$BASE/healthz" >/dev/null; then READY=1; break; fi
  sleep 0.1
done
[ "$READY" = 1 ] || { echo "server did not become ready"; cat /tmp/gangd-demo.stderr; exit 1; }

req() { # METHOD PATH [JSON_BODY]
  local method="$1" path="$2" body="${3:-}"
  {
    echo ""
    echo "### $ ${method} ${BASE}${path}"
    if [ -n "$body" ]; then echo "$body" | jq . ; fi
    echo "### response:"
  } >> "$LOG"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body" \
      | jq . >> "$LOG"
  else
    curl -sS -X "$method" "$BASE$path" | jq . >> "$LOG"
  fi
}

echo "# 1. register two nodes, 6 slots each"
req POST /v1/nodes '{"name":"n1","capacity":6,"labels":{"zone":"a"}}'
req POST /v1/nodes '{"name":"n2","capacity":6,"labels":{"zone":"b"}}'

echo "# 2. gang A reserves 4+4 slots (8 of 12)"
req POST /v1/gangs '{"id":"A","ttl_ms":5000,"tasks":[{"id":"a1","slots":4,"match_labels":{"zone":"a"}},{"id":"a2","slots":4,"match_labels":{"zone":"b"}}]}'

echo "# 3. gang B wants the same -> must WAIT (all-or-nothing, no partial hold)"
req POST /v1/gangs '{"id":"B","ttl_ms":5000,"tasks":[{"id":"b1","slots":4,"match_labels":{"zone":"a"}},{"id":"b2","slots":4,"match_labels":{"zone":"b"}}]}'

echo "# 4. capture A plan versions, then take node n1 OFFLINE before commit"
VERSIONS="$(curl -sS "$BASE/v1/gangs/A" | jq -c '.reservation.node_versions')"
echo "# A plan versions: $VERSIONS" | tee -a "$LOG"
req POST /v1/nodes/n1/status '{"status":"OFFLINE"}'

echo "# 5. A commits the stale plan -> expect 409 VERSION_MISMATCH, A FAILED"
req POST /v1/gangs/A/commit "{\"expected_node_versions\":$VERSIONS}"

echo "# 6. state: no running, no reserved (no leak / no double occupancy)"
req GET /v1/state

echo "# 7. n1 back ONLINE; FIFO promotes B; B commits both tasks atomically"
req POST /v1/nodes/n1/status '{"status":"ONLINE"}'
req GET  /v1/gangs/B
BVERS="$(curl -sS "$BASE/v1/gangs/B" | jq -c '.reservation.node_versions')"
req POST /v1/gangs/B/commit "{\"expected_node_versions\":$BVERS}"

echo "# 8. release B; A replans and commits successfully"
req POST /v1/gangs/B/release
req POST /v1/gangs/A/replan
AVERS="$(curl -sS "$BASE/v1/gangs/A" | jq -c '.reservation.node_versions // empty')"
req POST /v1/gangs/A/commit "{\"expected_node_versions\":$AVERS}"

echo "# 9. TTL expiry demo: gang C reserves with 300ms ttl, never commits"
req POST /v1/gangs '{"id":"C","ttl_ms":300,"tasks":[{"id":"c1","slots":1}]}'
echo "# waiting 1s for the reaper..."
sleep 1
req GET /v1/gangs/C

echo "# 10. final state"
req GET /v1/state

echo "# done. server log:"
cat /tmp/gangd-demo.stderr >> "$LOG"
echo "output written to $LOG"
