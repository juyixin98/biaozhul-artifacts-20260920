#!/usr/bin/env bash
# Manual end-to-end demonstration, runnable in one shot:
#   1. starts the server with a small series budget
#   2. ingests legitimate samples
#   3. runs a high-cardinality attack via cmd/synthgen
#   4. verifies bounded footprint + count conservation
#   5. stops the server, restarts it, verifies recovery from snapshot
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${1:-18200}"
BASE="http://127.0.0.1:${PORT}"
DATA="$(mktemp -d)"
LOG="$(mktemp)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$DATA"' EXIT

echo "== building binaries =="
go build -o /tmp/cb-server ./cmd/server
go build -o /tmp/cb-synthgen ./cmd/synthgen

echo "== data dir: $DATA =="
/tmp/cb-server -addr "127.0.0.1:${PORT}" -data-dir "$DATA" \
  -max-series 100 -max-metrics 50 -flush-interval 1s >"$LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null && break
  sleep 0.1
done
echo "== health =="
curl -sS "$BASE/healthz"; echo

echo
echo "== ingest: 3 legitimate samples (examples/ingest.json) =="
curl -sS -X POST "$BASE/api/v1/ingest" -H 'Content-Type: application/json' \
  -d @examples/ingest.json | head -c 600; echo

echo
echo "== attack: synthgen, 50 waves x 200 samples x 8 workers =="
/tmp/cb-synthgen -addr "$BASE" -mode attack -metric http_requests \
  -waves 50 -batch 200 -workers 8

echo
echo "== truncate scenario on a second metric =="
/tmp/cb-synthgen -addr "$BASE" -mode truncate -metric huge_labels \
  -waves 2 -batch 50 -workers 2 | tail -2

echo
echo "== stats after attack (expect series_total capped, overflow large) =="
STATS_AFTER="$(curl -sS "$BASE/api/v1/stats")"
echo "$STATS_AFTER" | python3 -m json.tool

echo
echo "== http_requests summary =="
curl -sS "$BASE/api/v1/metrics/http_requests" | python3 -m json.tool

echo
echo "== count conservation check (via python) =="
echo "$STATS_AFTER" | BASE="$BASE" python3 -c '
import json,sys,os,urllib.request
s=json.load(sys.stdin)
assert s["samples_received"] == s["samples_accepted"] + s["samples_rejected"], s
assert s["samples_accepted"] == s["normal_samples"] + s["overflow_samples"], s
m=json.load(urllib.request.urlopen(os.environ["BASE"]+"/api/v1/metrics/http_requests?series=1"))
per_series=sum(x["count"] for x in m["series"])
assert per_series == m["count"], (per_series, m["count"])
assert m["normal_series"] <= 100
print("CONSERVED: received=%d accepted=%d (normal=%d overflow=%d); metric series=%d+overflow, sum=%d"
      % (s["samples_received"], s["samples_accepted"], s["normal_samples"],
         s["overflow_samples"], m["normal_series"], per_series))
'

echo
echo "== graceful shutdown (SIGTERM) =="
kill -TERM "$SERVER_PID"
wait "$SERVER_PID" && echo "server exited 0"
SERVER_PID=""
echo "-- snapshot file --"
ls -la "$DATA"
echo "-- server log --"
cat "$LOG"

echo
echo "== restart from same data dir =="
/tmp/cb-server -addr "127.0.0.1:${PORT}" -data-dir "$DATA" \
  -max-series 100 -max-metrics 50 -flush-interval 1s >"$LOG" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null && break
  sleep 0.1
done

echo "== stats after restart (must match pre-restart) =="
STATS_RESTART="$(curl -sS "$BASE/api/v1/stats")"
echo "$STATS_RESTART" | python3 -m json.tool

python3 - "$STATS_AFTER" "$STATS_RESTART" <<'PY'
import json, sys
before=json.loads(sys.argv[1]); after=json.loads(sys.argv[2])
keys=["samples_received","samples_accepted","samples_rejected",
      "overflow_samples","normal_samples","truncated_label_values",
      "metric_names","series_total","overflow_series_total"]
diffs=[(k,before[k],after[k]) for k in keys if before[k]!=after[k]]
if diffs:
    print("MISMATCH:", diffs); sys.exit(1)
print("RECOVERY EXACT for:", ", ".join(keys))
PY

echo
echo "== post-restart: stable combo still in place, new combo overflows =="
curl -sS -X POST "$BASE/api/v1/ingest" -H 'Content-Type: application/json' \
  -d '{"samples":[
    {"metric":"http_requests","labels":{"method":"POST","path":"/api/login","status":"401","host":"host-02"},"value":1},
    {"metric":"http_requests","labels":{"request_id":"brand-new-after-restart"},"value":1}
  ]}' | python3 -c 'import json,sys; r=json.load(sys.stdin); print("accepted=%d overflow=%d (first sample hits a restored series, second overflows)"%(r["accepted"],r["overflow"]))'

echo
echo "ALL MANUAL CHECKS PASSED"
