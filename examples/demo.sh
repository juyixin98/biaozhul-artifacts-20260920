#!/usr/bin/env bash
# End-to-end manual demo against a server started with a TINY series budget
# so overflow is visible after only a handful of requests.
#
#   go run ./cmd/server -addr :8090 -data-dir ./data-demo -max-series 5
#   ./examples/demo.sh 8090
set -euo pipefail

PORT="${1:-8080}"
BASE="http://127.0.0.1:${PORT}"

say() { printf '\n=== %s ===\n' "$*"; }

say "health"
curl -sS "${BASE}/healthz"; echo

say "ingest legitimate traffic (3 stable combos)"
curl -sS -X POST "${BASE}/api/v1/ingest" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/ingest.json"; echo

say "ingest high-cardinality attack samples (unique request_id each)"
curl -sS -X POST "${BASE}/api/v1/ingest" \
  -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/attack.json"; echo

say "flood 50 more unique combos to guarantee overflow"
python3 - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
samples = [{
    "metric": "http_requests",
    "labels": {"path": "/api/users", "request_id": f"flood-{i}-{i*7919}"},
    "value": 1,
} for i in range(50)]
req = urllib.request.Request(
    base + "/api/v1/ingest",
    data=json.dumps({"samples": samples}).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as r:
    body = json.load(r)
print(json.dumps({k: body[k] for k in ("received", "accepted", "rejected", "overflow")}, indent=2))
PY

say "global stats (note overflow_samples and series_total bounded)"
curl -sS "${BASE}/api/v1/stats"; echo

say "metric detail WITHOUT per-series payload"
curl -sS "${BASE}/api/v1/metrics/http_requests"; echo

say "metric detail WITH per-series payload (budget + one overflow bucket)"
curl -sS "${BASE}/api/v1/metrics/http_requests?series=1" | python3 -m json.tool

say "force a snapshot flush"
curl -sS -X POST "${BASE}/debug/flush"; echo

say "snapshot file on disk:"
ls -la "$(dirname "$0")/../data-demo" || true
