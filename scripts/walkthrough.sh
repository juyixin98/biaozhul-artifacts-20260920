#!/usr/bin/env bash
# End-to-end HTTP walkthrough for dynamic-rule-binding.
#
# Starts the service in fully deterministic mode (simulated clock + manual
# maintenance scheduler), then exercises: bootstrap, hot update, boundary
# event, out-of-order late events, missing-version rejection, rollback,
# GC preconditions (preview, reference pinning, purge-then-reclaim), and the
# VERSION_RECLAIMED rejection.
#
# Usage: scripts/walkthrough.sh [port]   (default: pick a free port)
set -euo pipefail

PORT="${1:-0}"
if [ "$PORT" = "0" ]; then
  PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
fi
BASE="http://127.0.0.1:${PORT}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CP="$ROOT/target/dynamic-rule-binding-1.0.0.jar:$(echo "$ROOT"/target/lib/*.jar | tr ' ' ':')"

# The sandbox may export a proxy; loopback must bypass it.
CURL=(curl -sS --noproxy '*')

echo "starting server on port $PORT (sim clock, manual scheduler)"
java -cp "$CP" com.example.drvb.server.HttpServerMain \
  --port="$PORT" --clock=sim --scheduler=manual \
  --sim-start=1790163000000 \
  --watermark-delay=0 --allowed-lateness=3600000 \
  --result-retention=86400000 --maintenance-period=60000 \
  > /tmp/drvb-server.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  if "${CURL[@]}" -fsS "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

say()    { echo; echo "== $* =="; }
postf()  { "${CURL[@]}" -f -X POST -H 'Content-Type: application/json' "$BASE$1" --data-binary "@$2"; echo; }
posts()  { "${CURL[@]}" -f -X POST -H 'Content-Type: application/json' "$BASE$1" --data-binary "$2"; echo; }
posth()  { # print HTTP status, body stays in /tmp/drvb-resp.json
  "${CURL[@]}" -o /tmp/drvb-resp.json -w '%{http_code}' \
    -X POST -H 'Content-Type: application/json' "$BASE$1" --data-binary "@$2"; }
getq()   { "${CURL[@]}" -f "$BASE$1"; echo; }

say "health"
getq /health

say "event BEFORE bootstrap -> rejected MISSING_VERSION (no silent latest-rules fallback)"
posts /events '{"id":"pre-1","type":"payment","eventTime":"2026-09-23T08:00:00Z","data":{"amount":999}}'

say "bootstrap v1"
postf /admin/bootstrap "$ROOT/examples/01-bootstrap.json"

say "hot update: publish v2 effective 10:00 and v3 effective 11:00 (in advance)"
postf /admin/rules/versions "$ROOT/examples/02-publish-v2.json"
posts /admin/rules/versions '{
  "versionId":"v3","effectiveFrom":"2026-09-23T11:00:00Z",
  "description":"relax to 300",
  "rules":[
    {"id":"big-amount","name":"amount-above-threshold","eventType":"payment",
     "action":"BLOCK","enabled":true,
     "condition":{"op":"all","conditions":[
       {"op":"gte","field":"amount","value":300},
       {"op":"in","field":"currency","values":["USD","EUR","GBP"]},
       {"op":"notExists","field":"flags.whitelisted"}]}},
    {"id":"eu-region","name":"eu-region-review","eventType":"payment","action":"REVIEW",
     "enabled":true,"condition":{"op":"startsWith","field":"region","value":"eu"}}]}'

say "rejected: publish v2 again (overlap at 10:00) -> HTTP 422"
code=$(posth /admin/rules/versions "$ROOT/examples/02-publish-v2.json")
echo "HTTP $code"; cat /tmp/drvb-resp.json; echo

say "boundary instant @10:00:00.000 binds v2; 09:59:59.999 binds v1"
posts /events '{"id":"at-boundary","type":"payment","eventTime":"2026-09-23T10:00:00Z","data":{"amount":250,"currency":"USD"}}'
posts /events '{"id":"just-before","type":"payment","eventTime":"2026-09-23T09:59:59.999Z","data":{"amount":150,"currency":"USD"}}'

say "batch: on-time + out-of-order events, nested payload and whitelist flag"
postf /events/batch "$ROOT/examples/04-batch.json"

say "advance simulated processing clock to 11:30, then one late event @10:40 -> historical v2"
posts /admin/clock '{"set":"2026-09-23T11:30:00Z"}' >/dev/null
postf /events "$ROOT/examples/03-event-late.json"
say "too-late: event @09:00 at processing 11:30 (60m horizon = 10:30)"
posts /events '{"id":"too-late-1","type":"payment","eventTime":"2026-09-23T09:00:00Z","data":{"amount":999,"currency":"USD"}}'

say "rollback to v1 content effective 14:00"
postf /admin/rules/rollback "$ROOT/examples/05-rollback.json"

say "results query by event-time range [09:00,10:00)"
getq "/results?from=2026-09-23T09:00:00Z&to=2026-09-23T10:00:00Z"

say "GC preview: versions referenced by retained results"
getq /admin/gc-preview

say "force watermark to 15:00 then maintenance: horizon met but refs still pin versions"
postf /admin/watermark "$ROOT/examples/06-watermark.json" >/dev/null
postf /admin/maintenance "$ROOT/examples/08-maintenance.json"

say "advance clock beyond the 24h retention window; the manual scheduler tick runs the maintenance cycle (purge + reclaim)"
postf /admin/clock "$ROOT/examples/07-clock-set.json"
postf /admin/tick "$ROOT/examples/08-maintenance.json"
say "GC state after the tick: only the newest version remains"
getq /admin/gc-preview

say "event in a reclaimed interval -> VERSION_RECLAIMED"
posts /events '{"id":"after-gc","type":"payment","eventTime":"2026-09-23T09:10:00Z","data":{"amount":150,"currency":"USD"}}'

say "versions timeline after GC and final stats"
getq /admin/rules/versions
getq /admin/stats

echo
echo "walkthrough complete (server log: /tmp/drvb-server.log)"
