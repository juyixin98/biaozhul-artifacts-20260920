#!/usr/bin/env bash
# End-to-end demo against a locally running server. Everything is in-process
# and loopback; no external system is contacted.
#
# Usage:
#   scripts/demo.sh [base-url]
set -uo pipefail

BASE="${1:-http://127.0.0.1:8080}"
OUT="results/demo"
mkdir -p "$OUT"

say()  { printf '\n=== %s ===\n' "$1"; }
post() { # $1 = json file, extra curl args after
  curl -sS -w '\nHTTP %{http_code} in %{time_total}s\n' \
    -H 'Content-Type: application/json' -X POST "$BASE/process" \
    --data @"$1" "${@:2}"
}

say "health"
curl -sS "$BASE/healthz"; echo

say "1) all leaves succeed"
post examples/01-all-succeed.json | tee "$OUT/01-all-succeed.txt"

say "2) first fatal failure cancels the other leaves (they are gated; fatal-c fails after 300ms)"
post examples/02-first-fatal-cancels.json | tee "$OUT/02-first-fatal.txt"

say "3) a non-fatal best-effort failure is tolerated"
post examples/03-non-fatal-tolerated.json | tee "$OUT/03-non-fatal.txt"

say "4) client disconnect terminates remaining computation"
# Start a request that blocks both downstreams, then abort curl after 0.3s.
curl -sS --max-time 0.3 -H 'Content-Type: application/json' -X POST \
  "$BASE/process" --data @examples/04-client-disconnect.json \
  | tee "$OUT/04-client-disconnect.txt" || true
echo "(curl exited nonzero as expected after aborting)"
# Give the server a moment to drain, then release the gate and inspect stats.
sleep 0.3
curl -sS "$BASE/admin/release?gate=hold" >/dev/null
sleep 0.2
curl -sS "$BASE/admin/close-idle-connections" >/dev/null
sleep 0.2
say "stats after disconnect (in_flight_requests and held_resources must be 0)"
curl -sS "$BASE/stats" | tee "$OUT/04-stats-after-disconnect.json"; echo

say "5) clock-driven per-call timeout (100ms) cancels its sibling"
post examples/05-fake-clock-timeout.json | tee "$OUT/05-timeout.txt"
curl -sS "$BASE/admin/release?gate=slow" >/dev/null

say "final stats"
curl -sS "$BASE/admin/close-idle-connections" >/dev/null
sleep 0.2
curl -sS "$BASE/stats" | tee "$OUT/06-final-stats.json"; echo
echo
echo "Demo artifacts written to $OUT/"
