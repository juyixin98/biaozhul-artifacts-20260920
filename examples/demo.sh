#!/usr/bin/env bash
#
# examples/demo.sh — end-to-end walkthrough against a running taskq server.
#
# Usage:
#   ./examples/demo.sh [base_url]
#
# Requires: curl, jq. A fresh server is assumed (clean data dir), because the
# claim step takes "the oldest PENDING task". Run it against an empty server.
set -euo pipefail

BASE="${1:-http://localhost:8080}"
C=(curl -sS)
j() { jq -C . ; }

hr() { printf '\n==================== %s ====================\n' "$1"; }

hr "1. health"
"${C[@]}" "$BASE/healthz" | j

hr "2. submit a task"
SUBMIT=$("${C[@]}" -X POST "$BASE/tasks" -H 'Content-Type: application/json' \
  -d '{"payload":{"url":"https://example.com/job","priority":3}}')
echo "$SUBMIT" | j
ID=$(echo "$SUBMIT" | jq -r .task.id)
echo "task id: $ID"

hr "3. worker w1 claims the task (attempt 1, lease token returned)"
CLAIM=$("${C[@]}" -X POST "$BASE/tasks/claim" -H 'Content-Type: application/json' \
  -d '{"worker_id":"w1"}')
echo "$CLAIM" | j
TOKEN=$(echo "$CLAIM" | jq -r .lease_token)
echo "lease token: $TOKEN"

hr "4a. heartbeat with a FORGED token -> 409 STALE_LEASE"
"${C[@]}" -o /tmp/demo_err.json -w 'HTTP %{http_code}\n' -X POST \
  "$BASE/tasks/$ID/heartbeat" -H 'Content-Type: application/json' \
  -d '{"worker_id":"w1","lease_token":"forged"}'
jq -C . /tmp/demo_err.json

hr "4b. heartbeat with the real token -> 200, lease extended"
"${C[@]}" -X POST "$BASE/tasks/$ID/heartbeat" -H 'Content-Type: application/json' \
  -d "{\"worker_id\":\"w1\",\"lease_token\":\"$TOKEN\"}" | j

hr "5. cancel vs complete race: fire both at the same instant"
# Both requests are launched concurrently; exactly one wins.
"${C[@]}" -X POST "$BASE/tasks/$ID/cancel" -o /tmp/demo_cancel.json -w 'cancel   -> HTTP %{http_code}\n' &
"${C[@]}" -X POST "$BASE/tasks/$ID/complete" -H 'Content-Type: application/json' \
  -d "{\"worker_id\":\"w1\",\"lease_token\":\"$TOKEN\",\"result\":{\"output\":42}}" \
  -o /tmp/demo_complete.json -w 'complete -> HTTP %{http_code}\n' &
wait
echo "-- cancel response:";   jq -C . /tmp/demo_cancel.json
echo "-- complete response:"; jq -C . /tmp/demo_complete.json

hr "6. final state (exactly one terminal outcome)"
"${C[@]}" "$BASE/tasks/$ID" | jq '.task | {id,state,attempt,result,version}'

# ---------------------------------------------------------------------------
# Timeout / redispatch: lease is 4s for the server started by demo instructions.
# ---------------------------------------------------------------------------
hr "7. submit + claim a second task, then let the lease EXPIRE"
ID2=$("${C[@]}" -X POST "$BASE/tasks" -H 'Content-Type: application/json' \
  -d '{"payload":"slow job"}' | jq -r .task.id)
OLD=$("${C[@]}" -X POST "$BASE/tasks/claim" -H 'Content-Type: application/json' \
  -d '{"worker_id":"old-w"}')
OLD_TOKEN=$(echo "$OLD" | jq -r .lease_token)
echo "old-w holds token $OLD_TOKEN; sleeping 5s (> lease TTL)..."
sleep 5
echo "-- status after expiry (sweeper or lazy read flips it to PENDING):"
"${C[@]}" "$BASE/tasks/$ID2" | jq '.task | {id,state,attempt,worker_id}'

hr "8. old worker's result is REJECTED after timeout"
"${C[@]}" -o /tmp/demo_old.json -w 'HTTP %{http_code}\n' -X POST \
  "$BASE/tasks/$ID2/complete" -H 'Content-Type: application/json' \
  -d "{\"worker_id\":\"old-w\",\"lease_token\":\"$OLD_TOKEN\",\"result\":\"stale\"}"
jq -C . /tmp/demo_old.json

hr "9. new worker claims (attempt 2) and completes"
NEW=$("${C[@]}" -X POST "$BASE/tasks/claim" -H 'Content-Type: application/json' \
  -d '{"worker_id":"new-w"}')
echo "$NEW" | jq '.task | {id,state,attempt,worker_id}'
NEW_TOKEN=$(echo "$NEW" | jq -r .lease_token)
"${C[@]}" -X POST "$BASE/tasks/$ID2/complete" -H 'Content-Type: application/json' \
  -d "{\"worker_id\":\"new-w\",\"lease_token\":\"$NEW_TOKEN\",\"result\":\"fresh\"}" \
  | jq '.task | {id,state,attempt,result,version}'

hr "10. list all tasks"
"${C[@]}" "$BASE/tasks" | jq '.tasks[] | {id,state,attempt,result}'

echo
echo "Demo complete. Restart the server against the SAME -data directory and"
echo "GET the ids above to confirm state survived: $ID  $ID2"
