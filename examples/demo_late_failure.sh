#!/usr/bin/env bash
# End-to-end curl walkthrough against the local demo server.
# Reproduces: old slow call fails LATE (after the breaker tripped and reached
# half-open); it must not affect the new generation; two probes then recover.
#
# Usage: ./examples/demo_late_failure.sh [base_url]
set -euo pipefail

BASE="${1:-http://127.0.0.1:18080}"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== reset =="
curl -s -X POST "$BASE/api/reset" | j

echo "== make the upstream stall (and fail when released): old slow call =="
curl -s -X POST "$BASE/api/upstream/behavior" -d '{"stall":true,"fail":true}' | j

echo "== start the old slow call in the background (still CLOSED) =="
curl -s -X POST "$BASE/api/call" >/tmp/old_call.json &
OLD_CURL=$!
sleep 0.3

echo "== switch upstream to immediate failure =="
curl -s -X POST "$BASE/api/upstream/behavior" -d '{"fail":true}' | j

echo "== three fast failures trip the breaker =="
for i in 1 2 3; do
  echo "-- failure call $i --"
  curl -s -X POST "$BASE/api/call" | j
done

echo "== state should be OPEN; an extra call is rejected with 503 =="
curl -s -o /tmp/rejected.json -w 'http_status=%{http_code}\n' -X POST "$BASE/api/call"
cat /tmp/rejected.json | j

echo "== advance virtual time past the 10s cool-down -> HALF_OPEN =="
curl -s -X POST "$BASE/api/clock/advance" -d '{"duration_ms":10000}' | j

echo "== release the stalled old call: its failure arrives LATE =="
curl -s -X POST "$BASE/api/upstream/release" | j
wait "$OLD_CURL"
echo "-- old slow call result (arrived in a newer generation) --"
cat /tmp/old_call.json | j

echo "== state must STILL be half_open; the stale failure changed nothing =="
curl -s "$BASE/api/state" | j

echo "-- switch the fake upstream back to healthy =="
curl -s -X POST "$BASE/api/upstream/behavior" -d '{"dynamic":false}' | j

echo "== two successful probes close the breaker =="
curl -s -X POST "$BASE/api/call" | j
curl -s -X POST "$BASE/api/call" | j

echo "== final state =="
curl -s "$BASE/api/state" | j

echo "== run the built-in acceptance scenarios over HTTP =="
for s in late_failure probe_contention cancel_is_not_failure; do
  echo "-- $s --"
  curl -s -X POST "$BASE/api/scenarios/$s/run" | j > "/tmp/scenario_$s.json"
  python3 -c "import json;d=json.load(open('/tmp/scenario_$s.json'));print('success=',d['success'],'pass=',d['data']['pass'],'steps=',len(d['data']['steps']),'failures=',d['data']['failures'])"
done
