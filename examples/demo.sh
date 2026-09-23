#!/usr/bin/env bash
# End-to-end demo: load examples/blocks.json, build snapshots, query history,
# prune, and show that pruned heights fail explicitly.
# Usage: ./examples/demo.sh [addr]   (default 127.0.0.1:8080)
set -euo pipefail

ADDR="${1:-127.0.0.1:8080}"
BASE="http://${ADDR}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== head before =="
curl -s "${BASE}/v1/head"; echo

echo "== loading blocks from examples/blocks.json =="
jq -c '.[]' "${HERE}/blocks.json" | while read -r block; do
  curl -s -X POST -H 'Content-Type: application/json' -d "${block}" "${BASE}/v1/blocks"
  echo
done

echo "== build snapshots at 4, 7, 10 =="
for h in 4 7 10; do
  curl -s -X POST -H 'Content-Type: application/json' -d "{\"height\": ${h}}" "${BASE}/v1/snapshots/build"; echo
done

echo "== snapshots available =="
curl -s "${BASE}/v1/snapshots" | jq .

echo "== state at height 6 (snapshot@4 + deltas 5..6) =="
curl -s "${BASE}/v1/state/6" | jq .

echo "== alice at height 10 =="
curl -s "${BASE}/v1/state/10/accounts/alice" | jq .

echo "== build two more snapshots to trigger pruning (keep=3) =="
curl -s -X POST -H 'Content-Type: application/json' -d '{"height": 8}' "${BASE}/v1/snapshots/build" >/dev/null
curl -s -X POST -H 'Content-Type: application/json' -d '{"height": 9}' "${BASE}/v1/snapshots/build" >/dev/null
curl -s "${BASE}/v1/snapshots" | jq .

echo "== query a pruned height (expect HTTP 410) =="
curl -s -o /dev/null -w 'state/2 -> HTTP %{http_code}\n' "${BASE}/v1/state/2"

echo "== reader timeout (expect HTTP 408) =="
curl -s -o /dev/null -w 'state/10?timeout_ms=0 -> HTTP %{http_code}\n' "${BASE}/v1/state/10?timeout_ms=0"
