#!/usr/bin/env bash
# End-to-end smoke demo against a locally running counterreset server.
# Starts the server on an ephemeral port with a temp data dir, exercises the
# API, and prints the responses. Requires: go, curl, jq.
set -euo pipefail

PORT="${PORT:-}"
if [ -z "$PORT" ]; then
  # Pick an ephemeral free port (Python is ubiquitous; fall back to a list).
  PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
fi
BASE="http://127.0.0.1:${PORT}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMPDATA="$(mktemp -d)"
trap 'kill ${SERVER_PID:-} 2>/dev/null || true; rm -rf "$TMPDATA"' EXIT

echo "== build =="
( cd "$DIR" && go build -o "$TMPDATA/counterreset" . )

echo "== run (data dir: $TMPDATA) =="
"$TMPDATA/counterreset" -addr "127.0.0.1:${PORT}" -data "$TMPDATA/data" &
SERVER_PID=$!

# Wait for readiness.
for _ in $(seq 1 50); do
  curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

echo; echo "== 1. health =="
curl -fsS "$BASE/healthz" | jq .

echo; echo "== 2. ingest a series with a reset (t=40: 100 -> 5) =="
curl -fsS -X POST "$BASE/api/v1/series" -H 'Content-Type: application/json' -d '{
  "metric": "http_requests_total",
  "labels": {"path": "/api"},
  "samples": [
    {"t": 0,  "value": 0},
    {"t": 10, "value": 40},
    {"t": 20, "value": 100},
    {"t": 30, "value": 160},
    {"t": 40, "value": 5},
    {"t": 50, "value": 25}
  ]
}' | jq .

echo; echo "== 3. query the full window [0,50] (increase should be 185) =="
curl -fsS "$BASE/api/v1/query?metric=http_requests_total&from=0&to=50&max_interval=1000" \
  | jq '.result[0] | {increase, min_increase, max_increase, resets, window_rate: .window_rate.per_second}'

echo; echo "== 4. boundary interpolation: window [5,45] cuts two pairs in half =="
curl -fsS "$BASE/api/v1/query?metric=http_requests_total&from=5&to=45&max_interval=1000" \
  | jq '.result[0] | {increase, min_increase, max_increase, interpolated: [.segments[] | select(.kind=="interpolated")]}'

echo; echo "== 5. negative value is rejected with 400 =="
curl -sS -o /tmp/cr_neg.json -w 'http_status=%{http_code}\n' -X POST "$BASE/api/v1/series" \
  -H 'Content-Type: application/json' \
  -d '{"metric":"bad","samples":[{"t":1,"value":-3}]}'
cat /tmp/cr_neg.json | jq .; rm -f /tmp/cr_neg.json

echo; echo "== 6. duplicate timestamp, conflicting value -> 409 =="
curl -sS -o /tmp/cr_dup.json -w 'http_status=%{http_code}\n' -X POST "$BASE/api/v1/series" \
  -H 'Content-Type: application/json' \
  -d '{"metric":"http_requests_total","labels":{"path":"/api"},"samples":[{"t":10,"value":999}]}'
cat /tmp/cr_dup.json | jq .; rm -f /tmp/cr_dup.json

echo; echo "== 7. query beyond the last sample is NOT extrapolated =="
curl -fsS "$BASE/api/v1/query?metric=http_requests_total&from=50&to=80&max_interval=1000" \
  | jq '.result[0] | {increase, has_data: .has_data, unobserved_after, window_rate}'

echo; echo "done"
