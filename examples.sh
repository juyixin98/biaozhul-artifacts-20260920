#!/usr/bin/env bash
# Request samples against a running server (default localhost:8080).
# Usage: ./examples.sh [base-url]
set -euo pipefail
BASE="${1:-http://localhost:8080}"
J='Content-Type: application/json'

echo '== 1. submit =='
curl -s -X POST "$BASE/tasks" -H "$J" -d '{"id":"t1","payload":"resize image","max_attempts":3}'; echo

echo '== 2. claim (attempt 1) =='
curl -s -X POST "$BASE/tasks/t1/claim" -H "$J" -d '{"worker":"workerA"}'; echo

echo '== 3. heartbeat =='
curl -s -X POST "$BASE/tasks/t1/heartbeat" -H "$J" -d '{"worker":"workerA","attempt":1}'; echo

echo '== 4. cancel wins the race =='
curl -s -X POST "$BASE/tasks/t1/cancel" -H "$J" -d '{}'; echo

echo '== 5. losing complete -> 409 terminal =='
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/tasks/t1/complete" -H "$J" \
  -d '{"worker":"workerA","attempt":1,"result":"done"}'

echo '== 6. timeout: force lease expiry, reclaim, stale result rejected =='
curl -s -X POST "$BASE/tasks" -H "$J" -d '{"id":"t2","payload":"send email"}' > /dev/null
curl -s -X POST "$BASE/tasks/t2/claim" -H "$J" -d '{"worker":"workerA"}' > /dev/null
curl -s -X POST "$BASE/admin/sweep" -H "$J" -d '{"now":"2030-01-01T00:00:00Z"}'; echo
curl -s -X POST "$BASE/tasks/t2/claim" -H "$J" -d '{"worker":"workerB"}'; echo
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/tasks/t2/complete" -H "$J" \
  -d '{"worker":"workerA","attempt":1,"result":"stale"}'

echo '== 7. current attempt completes =='
curl -s -X POST "$BASE/tasks/t2/complete" -H "$J" \
  -d '{"worker":"workerB","attempt":2,"result":"sent"}'; echo

echo '== 8. list all =='
curl -s "$BASE/tasks"; echo
