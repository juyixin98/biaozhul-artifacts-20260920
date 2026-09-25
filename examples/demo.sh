#!/usr/bin/env bash
# examples/demo.sh — end-to-end acceptance walkthrough against a running
# dynpoold instance. Uses only curl + jq (jq optional; JSON is printed raw).
#
# Scenario:
#   1. create pool workers=3, queue=8, reject=abort
#   2. submit three blocking tasks (occupy all workers)
#   3. queue several sleep tasks
#   4. shrink 3 -> 1 while blocks run (workers marked retiring, none exits yet)
#   5. release blockers -> queued tasks drain, exactly 1 worker remains
#   6. graceful shutdown -> 0 workers, all accepted tasks completed
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
JQ="$(command -v jq || true)"
pretty() { if [ -n "$JQ" ]; then "$JQ" .; else cat; fi; }
req() { # METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  echo ">>> $method $path ${body}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" -H 'Content-Type: application/json' -d "$body" "$BASE$path" | pretty
  else
    curl -sS -X "$method" "$BASE$path" | pretty
  fi
  echo
}

echo "=== 1. create pool demo: 3 workers, queue 8, abort ==="
req POST /v1/pools '{"name":"demo","workers":3,"queue_size":8,"reject_policy":"abort"}'

echo "=== 2. three blocking tasks occupy every worker ==="
for n in 1 2 3; do
  req POST /v1/pools/demo/tasks "{\"type\":\"block\",\"id\":\"blk$n\",\"name\":\"blk$n\"}"
done
sleep 0.2
req GET /v1/pools/demo

echo "=== 3. queue five sleep tasks ==="
for n in 1 2 3 4 5; do
  req POST /v1/pools/demo/tasks '{"type":"sleep","sleep_ms":50}'
done
sleep 0.1
req GET /v1/pools/demo

echo "=== 4. shrink 3 -> 1 while blockers run ==="
req POST /v1/pools/demo/workers '{"workers":1}'
sleep 0.2
req GET /v1/pools/demo

echo "=== 5. release blockers; queued sleeps must all run, 1 worker stays ==="
for n in 1 2 3; do
  req POST "/v1/blocks/demo/release?name=blk$n"
done
sleep 0.6
req GET /v1/pools/demo

echo "=== 6. graceful shutdown ==="
req DELETE /v1/pools/demo
echo
echo "=== events (tail) ==="
req GET /v1/pools/demo/events
