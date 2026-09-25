#!/usr/bin/env bash
# demo.sh — end-to-end acceptance walkthrough against a running server.
#
# Exercises, over HTTP with only the provided fixture commands:
#   1. shuffled arrival  -> same summary as in-order (arrival-order independent)
#   2. duplicates replay -> fully idempotent
#   3. executor crash    -> incomplete, NOT passed
#   4. retry after crash -> run completes passed
#   5. late write        -> stale result never overwrites the newer attempt
#   6. missing results   -> incomplete, missing tests never counted passed
#   7. cancellation      -> explicit cancelled status
#
# Usage: scripts/demo.sh [base-url]
set -euo pipefail

BASE="${1:-http://127.0.0.1:8099}"
CURL="curl -sS"
j() { jq -r "$1"; }

post() { # path json-file
  curl -sS -X POST -H 'Content-Type: application/json' --data-binary "@$2" "$BASE$1"
}

status_field() { # runID jq-filter
  curl -sS "$BASE/runs/$1" | jq -r "$2"
}

echo "== health =="
$CURL "$BASE/healthz" | jq -c .

echo
echo "== 1. shuffled arrival must equal in-order =="
echo -n '{"run_id":"demo-shuffled"}' > /tmp/tm-shuffled.json
post /runs /tmp/tm-shuffled.json >/dev/null
echo -n '{"command":["gen_events.sh","shuffled"]}' > /tmp/tm-exec.json
post /runs/demo-shuffled/execute /tmp/tm-exec.json > /tmp/tm-shuffled-res.json
jq '{ingested, duplicates, status: .summary.status, counts: .summary.counts}' /tmp/tm-shuffled-res.json

echo -n '{"run_id":"demo-ordered"}' > /tmp/tm-ordered.json
post /runs /tmp/tm-ordered.json >/dev/null
echo -n '{"command":["gen_events.sh","happy"]}' > /tmp/tm-exec.json
post /runs/demo-ordered/execute /tmp/tm-exec.json > /tmp/tm-ordered-res.json
jq '{status: .summary.status, counts: .summary.counts}' /tmp/tm-ordered-res.json

a=$(jq -Sc '.summary.counts, .summary.status' /tmp/tm-shuffled-res.json)
b=$(jq -Sc '.summary.counts, .summary.status' /tmp/tm-ordered-res.json)
[ "$a" = "$b" ] && echo "MATCH: shuffled == in-order" || { echo "MISMATCH"; exit 1; }

echo
echo "== 2. replay all events twice: idempotent =="
$CURL "$BASE/runs/demo-shuffled/events" \
  | jq '{events: [.events[] | select(.type != "run_started")]}' \
  > /tmp/tm-replay.json
post /runs/demo-shuffled/replay /tmp/tm-replay.json | jq '{accepted, duplicates, status: .summary.status}'
post /runs/demo-shuffled/replay /tmp/tm-replay.json | jq '{accepted, duplicates, status: .summary.status}'

echo
echo "== 3. executor crash: incomplete, never passed =="
echo -n '{"run_id":"demo-crash"}' > /tmp/tm-c.json
post /runs /tmp/tm-c.json >/dev/null
echo -n '{"command":["gen_events.sh","crash"],"execution_id":"exec-1"}' > /tmp/tm-exec.json
post /runs/demo-crash/execute /tmp/tm-exec.json > /tmp/tm-crash-res.json
jq '{exit_code: .record.exit_code, ingested, status: .summary.status, missing_finish: .summary.missing_finish, counts: .summary.counts}' /tmp/tm-crash-res.json

echo
echo "== 3b. crash replay with same execution_id: 0 new events =="
post /runs/demo-crash/execute /tmp/tm-exec.json | jq '{ingested, duplicates, counts: .summary.counts}'

echo
echo "== 4. retry executor finishes the missing test =="
cat > /tmp/tm-fix.json <<'JSON'
{"events":[
 {"event_id":"d1f","seq_no":5,"type":"attempt_finished","shard":"shard-a","test_id":"test-delta","attempt_id":"d-2","attempt_no":2,"status":"passed"},
 {"event_id":"rf2","seq_no":6,"type":"run_finished","status":"passed"}
]}
JSON
post /runs/demo-crash/replay /tmp/tm-fix.json | jq '{accepted, status: .summary.status, counts: .summary.counts}'

echo
echo "== 5. late write to old attempt is ignored =="
echo -n '{"run_id":"demo-late"}' > /tmp/tm-c.json
post /runs /tmp/tm-c.json >/dev/null
echo -n '{"command":["gen_events.sh","late-write"]}' > /tmp/tm-exec.json
post /runs/demo-late/execute /tmp/tm-exec.json > /tmp/tm-late-res.json
jq '{status: .summary.status, late_writes: .summary.late_writes_total, test: [.summary.tests[] | select(.test_id=="test-late") | {status, latest: .latest_attempt_id, old_attempt: [.attempts[] | select(.attempt_id=="t-1") | {status, late_writes}]}]}' /tmp/tm-late-res.json

echo
echo "== 6. missing results: never passed =="
echo -n '{"run_id":"demo-miss","tests":["test-maybe","test-ghost"]}' > /tmp/tm-c.json
post /runs /tmp/tm-c.json >/dev/null
echo -n '{"command":["gen_events.sh","missing"]}' > /tmp/tm-exec.json
post /runs/demo-miss/execute /tmp/tm-exec.json >/dev/null
$CURL "$BASE/runs/demo-miss" | jq '{status, counts, tests: [.tests[] | {test_id, status}]}'

echo
echo "== 7. cancellation =="
echo -n '{"run_id":"demo-cancel"}' > /tmp/tm-c.json
post /runs /tmp/tm-c.json >/dev/null
echo -n '{"command":["gen_events.sh","cancelled"]}' > /tmp/tm-exec.json
post /runs/demo-cancel/execute /tmp/tm-exec.json >/dev/null
$CURL "$BASE/runs/demo-cancel" | jq '{status, cancelled, counts}'

echo
echo "== 8. bad input is rejected, not persisted =="
echo -n '{"run_id":"demo-valid"}' > /tmp/tm-c.json
post /runs /tmp/tm-c.json >/dev/null
echo -n '{"event_id":"bad1","seq_no":1,"type":"attempt_finished","shard":"a","test_id":"t","attempt_id":"x","attempt_no":1,"status":"bogus"}' > /tmp/tm-bad.json
echo -n "HTTP " ; curl -sS -o /tmp/tm-bad-res.json -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' --data-binary @/tmp/tm-bad.json "$BASE/runs/demo-valid/events"
cat /tmp/tm-bad-res.json | jq -c .
echo -n '{"command":["../../../../etc/passwd"]}' > /tmp/tm-escape.json
echo -n "HTTP " ; curl -sS -o /tmp/tm-escape-res.json -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' --data-binary @/tmp/tm-escape.json "$BASE/runs/demo-valid/execute"
cat /tmp/tm-escape-res.json | jq -c .

echo
echo "== summary repeatability: GET 3 times, compare =="
for i in 1 2 3; do $CURL "$BASE/runs/demo-crash" | jq -Sc '{status, counts, missing_finish, late: .late_writes_total}'; done

echo
echo "== runs list =="
$CURL "$BASE/runs" | jq -c .
echo
echo "DEMO OK"
