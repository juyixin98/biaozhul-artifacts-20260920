#!/usr/bin/env bash
# End-to-end request examples for the dynamic thread-pool service.
#
# Usage:
#   go run ./cmd/dynpoold -addr 127.0.0.1:8080 -workers 4 -queue 8 -policy abort
#   ./examples/requests.sh            # runs the happy path
#   FORCE=1 ./examples/requests.sh    # uses force shutdown
#
# Requires: curl, jq (jq is optional; falls back to raw JSON).
set -u

BASE="${BASE:-http://127.0.0.1:8080}"
JQ="$(command -v jq || true)"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
# pretty-print JSON if jq is present
pp() { if [ -n "$JQ" ]; then "$JQ" .; else cat; fi; }

say "health"
curl -s "$BASE/healthz" | pp

say "initial stats (4 workers, queue cap 8)"
curl -s "$BASE/v1/pool" | pp

say "submit two quick tasks"
curl -s -X POST "$BASE/v1/tasks" -H 'Content-Type: application/json' \
  -d '{"id":"a1","type":"demo","sleep_ms":20}' | pp
curl -s -X POST "$BASE/v1/tasks" -H 'Content-Type: application/json' \
  -d '{"id":"a2","type":"demo","sleep_ms":40,"fail":false}' | pp

say "submit a failing task"
curl -s -X POST "$BASE/v1/tasks" -H 'Content-Type: application/json' \
  -d '{"id":"a3","fail":true}' | pp

sleep 0.2
say "fetch task a1"
curl -s "$BASE/v1/tasks/a1" | pp

say "fill the pool deterministically to trigger a rejection"
# 4 workers: submit 4 long tasks and wait until all 4 are running.
for i in 1 2 3 4; do
  curl -s -o /dev/null -X POST "$BASE/v1/tasks" \
    -H 'Content-Type: application/json' -d "{\"id\":\"run-$i\",\"sleep_ms\":3000}"
done
for _ in $(seq 1 50); do
  running=$(curl -s "$BASE/v1/pool" | "$JQ" '.running_now')
  [ "$running" = "4" ] && break
  sleep 0.05
done
echo "running_now=$(curl -s "$BASE/v1/pool" | "$JQ" .running_now)"
# Fill the remaining queue (capacity 8) with 8 more blocked tasks.
for i in 5 6 7 8 9 10 11 12; do
  curl -s -o /dev/null -w "fill-$i -> HTTP %{http_code}\n" -X POST "$BASE/v1/tasks" \
    -H 'Content-Type: application/json' -d "{\"id\":\"run-$i\",\"sleep_ms\":3000}"
done
echo "queue_len=$(curl -s "$BASE/v1/pool" | "$JQ" .queue_len) (expect 8)"

say "the next submit is REJECTED (queue full) -> HTTP 503"
curl -s -X POST "$BASE/v1/tasks" \
  -H 'Content-Type: application/json' -d '{"id":"overflow"}' | pp
curl -s -o /dev/null -w "rejection status -> HTTP %{http_code}\n" -X POST "$BASE/v1/tasks" \
  -H 'Content-Type: application/json' -d '{"id":"overflow2"}'

say "grow pool 4 -> 10 while tasks are blocked"
curl -s -X POST "$BASE/v1/pool/resize" -H 'Content-Type: application/json' \
  -d '{"workers":10}' | pp

say "shrink pool 10 -> 2 (running tasks are NOT interrupted)"
curl -s -X POST "$BASE/v1/pool/resize" -H 'Content-Type: application/json' \
  -d '{"workers":2}' | pp
say "structured events (truncated view)"
if [ -n "$JQ" ]; then
  curl -s "$BASE/v1/events" | "$JQ" '.events[0:12]'
else
  curl -s "$BASE/v1/events" | head -c 1200; echo
fi

say "shutdown"
if [ "${FORCE:-0}" = "1" ]; then
  echo "(force=1: pending/blocked tasks are canceled, running ctx interrupted)"
  curl -s -X POST "$BASE/v1/pool/shutdown?force=1&timeout_ms=5000" | pp
else
  echo "(graceful: waits for all accepted blocked tasks to finish, ~5s)"
  curl -s -X POST "$BASE/v1/pool/shutdown?timeout_ms=30000" | pp
fi

say "submit after shutdown -> HTTP 409"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/v1/tasks" \
  -H 'Content-Type: application/json' -d '{"id":"late"}'

echo
echo "Done. Pool process exits after termination."
