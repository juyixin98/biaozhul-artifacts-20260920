#!/usr/bin/env bash
# End-to-end demo: start the server with a TINY cluster capacity and prove
# LRU eviction, query filtering, keyword separation and snapshot persistence.
# Uses only curl + the two binaries. Deterministic: time stamps are part of
# the request bodies, not generated here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/bin/logcluster"
ADDR="127.0.0.1:18654"
DATA="$(mktemp -d)"
SNAP="$DATA/snap.json"
BASE="http://$ADDR"

echo "== data dir: $DATA"
"$BIN" -addr "$ADDR" -snapshot "$SNAP" -max-clusters 4 -ring-capacity 200 -autosave 0 \
  >"$DATA/server.log" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

wait_ready() {
  local logf="$1"
  for i in $(seq 1 50); do
    if curl -sf --noproxy '*' "$BASE/healthz" >/dev/null 2>&1; then return 0; fi
    sleep 0.1
  done
  echo "server did not become ready; log:" >&2
  cat "$logf" >&2 || true
  exit 1
}
wait_ready "$DATA/server.log"

post() { curl -s --noproxy '*' -X POST -H 'Content-Type: application/json' "$@"; }
get()  { curl -s --noproxy '*' "$@"; }

# postj URL -d '...' sends one JSON body and prints the first assignment.
postj() { local url="$1"; shift; post "$url" "$@" | jq -c '.results[0] | {cluster_id, template}'; }

echo; echo "== ingest 4 distinct templates (fills capacity 4)"
postj "$BASE/v1/ingest" -d '{"line":"10:00:01 alpha service started pid 1001"}'
postj "$BASE/v1/ingest" -d '{"line":"10:00:02 beta service started pid 1002"}'
postj "$BASE/v1/ingest" -d '{"line":"10:00:03 gamma service started pid 1003"}'
postj "$BASE/v1/ingest" -d '{"line":"10:00:04 delta service started pid 1004"}'

echo; echo "== touch clusters 2,3,4 again so cluster 1 (alpha) becomes LRU"
postj "$BASE/v1/ingest" -d '{"line":"10:00:05 beta service started pid 1005"}'  >/dev/null
postj "$BASE/v1/ingest" -d '{"line":"10:00:06 gamma service started pid 1006"}' >/dev/null
postj "$BASE/v1/ingest" -d '{"line":"10:00:07 delta service started pid 1007"}' >/dev/null

echo; echo "== ingest a 5th distinct template -> cluster 1 must be evicted (LRU)"
postj "$BASE/v1/ingest" -d '{"line":"10:00:08 epsilon totally different shape now"}'

echo; echo "== evicted templates"
get "$BASE/v1/evicted" | jq -c '.evicted[] | {cluster_id, template, count}'

echo; echo "== keyword separation: refused vs timed out are different templates"
postj "$BASE/v1/ingest" -d '{"line":"10:00:09 conn refused to host now"}' >/dev/null
postj "$BASE/v1/ingest" -d '{"line":"10:00:10 conn timeout to host now"}' >/dev/null
get "$BASE/v1/templates" | jq -r '.templates[] | "  c\(.id) v\(.version) n=\(.count): \(.template)"'

echo; echo "== query filter: only lines containing 'refused'"
get "$BASE/v1/events?q=refused" | jq -r '.events[] | "  seq\(.seq) c\(.cluster_id): \(.line)"'

echo; echo "== metrics"
get "$BASE/v1/metrics" | jq -c '.'

echo; echo "== snapshot, restart, verify templates survive"
post "$BASE/v1/snapshot" -d "{\"path\":\"$SNAP\"}" | jq -c '.'
kill $SRV; wait $SRV 2>/dev/null || true
"$BIN" -addr "$ADDR" -snapshot "$SNAP" -max-clusters 4 -ring-capacity 200 -autosave 0 \
  >"$DATA/server2.log" 2>&1 &
SRV=$!
wait_ready "$DATA/server2.log"
get "$BASE/v1/metrics" | jq -c '{total_lines, active_clusters, stored_events}'
echo "  restart log: $(grep restored "$DATA/server2.log")"

echo; echo "DEMO OK"
