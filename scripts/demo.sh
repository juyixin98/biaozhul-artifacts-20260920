#!/usr/bin/env bash
# End-to-end demo for sse-resume. Requires: curl, Go >=1.23.
set -u
PORT="${PORT:-$(( ( RANDOM % 20000 ) + 20000 ))}"
BASE="http://127.0.0.1:${PORT}"
DATA_DIR="$(mktemp -d)"
MAX=20

echo "== data dir: ${DATA_DIR}"
BIN_DIR="$(mktemp -d)"
go build -o "${BIN_DIR}/sse-server" ./cmd/server
go build -o "${BIN_DIR}/demo-client" ./cmd/demo-client
trap 'kill "${SRV}" 2>/dev/null; wait "${SRV}" 2>/dev/null; rm -rf "${DATA_DIR}" "${BIN_DIR}"' EXIT
"${BIN_DIR}/sse-server" -addr ":${PORT}" -data "${DATA_DIR}" -max-events ${MAX} \
  -queue 64 -heartbeat 2s -write-timeout 3s >/tmp/sse-demo-server.log 2>&1 &
SRV=$!

wait_http() {
  for _ in $(seq 1 50); do
    curl -sf "${BASE}/healthz" >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  echo "server did not come up"; exit 1
}
wait_http
echo "== server up on ${BASE}"

echo; echo "== 1) publish 22 events (retention=${MAX}, so oldest should slide to 3)"
for i in $(seq 1 22); do
  curl -sf -X POST "${BASE}/v1/events" -H 'Content-Type: application/json' \
    -d "{\"event\":\"msg\",\"data\":\"event number ${i}\"}" >/dev/null
done
curl -s "${BASE}/v1/stats"; echo

echo; echo "== 2) stale cursor Last-Event-ID: 1 -> expect event: reset / cursor_expired"
curl -sN --max-time 2 -H 'Last-Event-ID: 1' "${BASE}/v1/events/stream" || true

echo; echo "== 3) fresh stream grabs retained events (first frames shown)"
curl -sN --max-time 1 "${BASE}/v1/events/stream" | head -8 || true

echo; echo "== 4) multi-line data wire encoding"
curl -sf -X POST "${BASE}/v1/events" -H 'Content-Type: application/json' \
  -d '{"event":"doc","data":"line-A\nline-B\n\nline-D after blank"}' >/dev/null
curl -sN --max-time 1 "${BASE}/v1/events/stream?after=22" | head -9 || true

echo; echo "== 5) 409 on stale non-streaming replay"
curl -s -o /tmp/sse-demo-409.json -w "HTTP %{http_code}\n" "${BASE}/v1/events?after=1"
cat /tmp/sse-demo-409.json; echo

echo; echo "== 6) demo client: runs 10s; the server is KILLED (SIGKILL, hard crash,"
echo "      every SSE connection drops instantly) and restarted from the same"
echo "      data dir, while 8 more events are published. The client must"
echo "      auto-reconnect with Last-Event-ID, dedupe seam duplicates, and"
echo "      end with a contiguous id set (no gaps)."
"${BIN_DIR}/demo-client" -url "${BASE}" -duration 10s -lag 3 >/tmp/sse-demo-client1.log 2>&1 &
CLI=$!
sleep 2
# Live event delivered over the open stream right before the crash.
curl -sf -X POST "${BASE}/v1/events" -H 'Content-Type: application/json' \
  -d '{"event":"msg","data":"pre-crash live event 24"}' >/dev/null
sleep 1
echo "-- killing server with SIGKILL (simulated crash) ..."
kill -9 "${SRV}"; wait "${SRV}" 2>/dev/null
"${BIN_DIR}/sse-server" -addr ":${PORT}" -data "${DATA_DIR}" -max-events ${MAX} \
  -queue 64 -heartbeat 2s -write-timeout 3s >>/tmp/sse-demo-server.log 2>&1 &
SRV=$!
wait_http
# Reconnect replays id 24 (already seen live) -> seam duplicate, deduped;
# then live ids 25..30 follow.
for i in 25 26 27 28 29 30; do
  curl -sf -X POST "${BASE}/v1/events" -H 'Content-Type: application/json' \
    -d "{\"event\":\"msg\",\"data\":\"post-restart ${i}\"}" >/dev/null
  sleep 0.3
done
wait "${CLI}" 2>/dev/null
grep -E 'connected|connection ended|server closed|dup |GAP|RESULT|unique events|duplicates|reconnects|resets|pings' /tmp/sse-demo-client1.log || true

echo; echo "== 7) persistence after restart: ids continue to 30 (oldest slid to 12)"
curl -s "${BASE}/v1/stats"; echo

echo; echo "== 8) validation errors"
curl -s -o /dev/null -w 'empty data -> HTTP %{http_code}\n' -X POST "${BASE}/v1/events" \
  -H 'Content-Type: application/json' -d '{"data":""}'
curl -s -o /dev/null -w 'bad cursor -> HTTP %{http_code}\n' \
  -H 'Last-Event-ID: abc' "${BASE}/v1/events/stream"

echo; echo "== logs:"
echo "-- server log (tail)"; tail -4 /tmp/sse-demo-server.log
echo "DONE"
