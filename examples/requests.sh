#!/usr/bin/env bash
# Request examples for the deadline-admission local HTTP server.
#
# Start the server first (script executor, bind localhost only):
#   go run ./cmd/server -addr 127.0.0.1:8080 -executor script -overrun kill_at_budget
#
# Then run:  bash examples/requests.sh
#
# Times below use deadline_rel_ms (relative to the server clock at submission)
# and budget_ms / sleep durations in milliseconds.
set -u
BASE="${BASE:-http://127.0.0.1:8080}"
say() { printf '\n===== %s =====\n' "$1"; }
j()   { python3 -m json.tool 2>/dev/null || cat; }

say "health"
curl -s "$BASE/healthz"; echo

say "submit a: sleep 300ms, declares 300, deadline in 2000ms (admitted, runs now)"
curl -s -X POST "$BASE/jobs" -H 'Content-Type: application/json' \
  -d '{"id":"a","payload":"sleep:300","demand":1,"deadline_rel_ms":2000,"budget_ms":300}' | j; echo

say "submit b: queued behind a (single capacity)"
curl -s -X POST "$BASE/jobs" -H 'Content-Type: application/json' \
  -d '{"id":"b","payload":"sleep:200","demand":1,"deadline_rel_ms":5000,"budget_ms":200}' | j; echo

say "submit c: queued, honest"
curl -s -X POST "$BASE/jobs" -H 'Content-Type: application/json' \
  -d '{"id":"c","payload":"sleep:400","demand":1,"deadline_rel_ms":2500,"budget_ms":400}' | j; echo

say "submit infeasible: 1000ms of work but only 800ms slack -> HTTP 422 (predicted)"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/jobs" -H 'Content-Type: application/json' \
  -d '{"id":"x","payload":"sleep:1000","demand":1,"deadline_rel_ms":800,"budget_ms":1000}' | j

say "cancel queued/running job b"
curl -s -X POST "$BASE/jobs/b/cancel" | j; echo

say "get one job"
curl -s "$BASE/jobs/a" | j; echo

say "list all jobs"
curl -s "$BASE/jobs" | j

say "structured events (optionally filter: ?job_id=a)"
curl -s "$BASE/events" | j

say "counters + resource conservation"
curl -s "$BASE/stats" | j

say "unknown job -> 404; double cancel -> 409"
curl -s -o /dev/null -w 'GET /jobs/nope -> HTTP %{http_code}\n' "$BASE/jobs/nope"
curl -s -o /dev/null -w 'second cancel b   -> HTTP %{http_code}\n' -X POST "$BASE/jobs/b/cancel"
