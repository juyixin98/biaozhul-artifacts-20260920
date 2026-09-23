#!/usr/bin/env bash
# Demo: start the ScL JSON service and exercise every endpoint with curl.
# Usage: ./examples/curl_demo.sh [PORT]
set -euo pipefail

PORT="${1:-8765}"
BASE="http://127.0.0.1:${PORT}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

echo ">> starting service on port ${PORT}"
python3 -m sclang.service --port "${PORT}" >/tmp/scl_service.log 2>&1 &
SVC_PID=$!
trap 'kill ${SVC_PID} 2>/dev/null || true' EXIT

# wait for the port
for _ in $(seq 1 50); do
  if curl -sf "${BASE}/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

echo; echo "== health =="
curl -s "${BASE}/healthz"; echo

echo; echo "== eval: shared-cell counter (reference vs converted VM) =="
curl -s -X POST "${BASE}/eval" -H 'Content-Type: application/json' \
  --data @"${HERE}/requests/eval_counter.json" | python3 -m json.tool

echo; echo "== analyze: transitive capture / boxing report =="
curl -s -X POST "${BASE}/analyze" -H 'Content-Type: application/json' \
  --data @"${HERE}/requests/analyze_share.json" | python3 -m json.tool

echo; echo "== compile: closure-converted IR module (summary) =="
curl -s -X POST "${BASE}/compile" -H 'Content-Type: application/json' \
  --data @"${HERE}/requests/compile_adder.json" \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["disassembly"])'

echo; echo "== lex: tokens with source spans =="
curl -s -X POST "${BASE}/lex" -H 'Content-Type: application/json' \
  -d '{"source":"let x = 42;"}' | python3 -m json.tool

echo; echo "== error: unbound variable returns span + snippet =="
curl -s -X POST "${BASE}/eval" -H 'Content-Type: application/json' \
  --data @"${HERE}/requests/eval_error_unbound.json" | python3 -m json.tool

echo; echo "== CLI differential check on all examples =="
for f in "${ROOT}"/examples/*.scl; do
  printf '%-40s ' "$(basename "$f")"
  python3 -m sclang.cli diff "$f" | tail -1
done
