#!/usr/bin/env bash
# End-to-end demo of the acceptance scenario over HTTP:
#   events at t=0, t=20, then the late t=10 with gap=10 -> bridge,
#   retract + new version, no double count, watermark rejection, idempotency.
#
# Usage:
#   ./scripts/run.sh            # in one terminal
#   ./scripts/examples.sh       # in another (set PORT if needed)
set -u
BASE="http://localhost:${PORT:-8080}"

say() { printf '\n==================== %s ====================\n' "$1"; }
post() { curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' -d "$1"; echo; }
get() { curl -s "$BASE$1"; echo; }

say "health"
get /health

say "1) event t=0  -> creates session k@0 v1"
post '{"eventId":"e0","key":"k","timestamp":0}'

say "2) event t=20 -> 20-0=20 > gap 10, creates session k@20 v1"
post '{"eventId":"e20","key":"k","timestamp":20}'

say "sessions BEFORE bridge (expect two: k@0, k@20)"
get /sessions

say "3) LATE event t=10 -> bridges both (10-0=10 and 20-10=10, <= gap)"
post '{"eventId":"e10","key":"k","timestamp":10}'

say "sessions AFTER bridge (expect ONE session k@0 v2 [e0,e10,e20])"
get /sessions

say "changelog (expect upsert k@0, upsert k@20, retract k@20, retract k@0v1, upsert k@0v2)"
get /changelog

say "accounting (materializedEventRows=3, noDoubleCount=true)"
get /accounting

say "watermark snapshot (maxObservedTs=20, watermark=10)"
get /watermark

say "4) idempotent replay of e10 (expect 200, duplicate:true, no new changes)"
post '{"eventId":"e10","key":"k","timestamp":10}'

say "5) same eventId with a DIFFERENT timestamp (expect HTTP 409)"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/events" \
  -H 'Content-Type: application/json' -d '{"eventId":"e10","key":"k","timestamp":11}'

say "6) batch ingest on ANOTHER key (watermark is 10; events must be >= 10)"
echo '   grp@10 and grp@15 merge (gap 5<=10); grp@50 is its own session.'
post '[{"eventId":"b1","key":"grp","timestamp":10},
      {"eventId":"b2","key":"grp","timestamp":15},
      {"eventId":"b3","key":"grp","timestamp":50}]'

say "7) push max event time to 100 (GLOBAL watermark -> 90)"
post '{"eventId":"hi","key":"k","timestamp":100}'

say "8) event t=5 is BELOW watermark 90 (expect HTTP 422, rejected)"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/events" \
  -H 'Content-Type: application/json' -d '{"eventId":"tooLate","key":"k","timestamp":5}'

say "9) event t=90 is EXACTLY at the watermark (accepted; bridges k@100 into k@90)"
post '{"eventId":"edge","key":"k","timestamp":90}'

say "rejected list (expect exactly one: tooLate@5)"
get /rejected

say "batch ingest on another key"
post '[{"eventId":"b1","key":"grp","timestamp":0},
      {"eventId":"b2","key":"grp","timestamp":5},
      {"eventId":"b3","key":"grp","timestamp":50}]'

say "final state"
get /state

cat <<'TIP'

Recovery check:
  1. Stop the server (Ctrl-C in its terminal).
  2. Start it again with the same DATA_DIR:   ./scripts/run.sh
  3. curl http://localhost:8080/sessions
     -> session ids, versions and event memberships are identical.
TIP
