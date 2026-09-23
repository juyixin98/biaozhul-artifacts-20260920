#!/usr/bin/env bash
# End-to-end demo: build, start the server on an ephemeral port, run the
# synthetic acceptance simulator, show persisted data, then restart the
# server to prove entries survive the restart, and finally shut down.
set -euo pipefail

cd "$(dirname "$0")/.."

DATA_DIR="$(mktemp -d)/logpipe-demo"
mkdir -p "$DATA_DIR"
trap 'for p in "${SRV_PID:-}" "${SRV2_PID:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done' EXIT

free_port() {
  python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
}

echo "== go build =="
go build -o bin/logpipe ./cmd/server
go build -o bin/simulator ./cmd/simulator

PORT="$(free_port)"
echo "== start server on 127.0.0.1:${PORT}, data dir ${DATA_DIR} =="
./bin/logpipe -addr "127.0.0.1:${PORT}" -data-dir "$DATA_DIR" \
  -timeout 1s -sweep-interval 100ms &
SRV_PID=$!
sleep 0.8

echo
echo "== health =="
curl -s "http://127.0.0.1:${PORT}/healthz"; echo

echo
echo "== acceptance simulator (interleaving / orphan / oversized / restart) =="
./bin/simulator -url "http://127.0.0.1:${PORT}" -wait 2s

echo
echo "== query by source (alpha only) =="
curl -s "http://127.0.0.1:${PORT}/entries?source=alpha" | python3 -m json.tool

echo "== persisted file =="
wc -l "${DATA_DIR}/entries.jsonl"

echo
echo "== graceful shutdown (pending entries flush with reason=shutdown) =="
kill -TERM "$SRV_PID"
wait "$SRV_PID" || true
SRV_PID=""

echo
echo "== restart with the same data dir: entries are replayed =="
PORT2="$(free_port)"
./bin/logpipe -addr "127.0.0.1:${PORT2}" -data-dir "$DATA_DIR" \
  -timeout 1s -sweep-interval 100ms &
SRV2_PID=$!
sleep 0.8
COUNT="$(curl -s "http://127.0.0.1:${PORT2}/entries" | python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])')"
echo "entries visible after restart: ${COUNT}"

kill -TERM "$SRV2_PID"
wait "$SRV2_PID" || true
SRV2_PID=""
echo
echo "demo finished OK; data kept at ${DATA_DIR}"
