#!/usr/bin/env bash
# Minimal service-call examples. Start the server first:
#
#   SPARSE_EMB_CONFIG=fixtures/config_small.json \
#   SPARSE_EMB_STATE_DIR=var/state \
#   uvicorn sparse_embedding.app:app --app-dir src --host 127.0.0.1 --port 8000
#
# Every request carries a run_id so journal lines can be correlated to input.

set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "1) health"
curl -s "$BASE/health" | python3 -m json.tool

echo "2) duplicate indices are aggregated (rows 0 and 2 each step once)"
curl -s -X POST "$BASE/apply" \
  -H 'Content-Type: application/json' \
  -d '{
        "run_id": "ex-dup-1", "batch_id": "dup",
        "indices": [0, 2, 0, 2, 2],
        "values": [[1,0],[0,2],[3,4],[-1,0],[1,1]]
      }' | python3 -m json.tool

echo "3) hot/cold + cancelling row (row 5 sums to zero -> skipped, no step)"
curl -s -X POST "$BASE/apply" \
  -H 'Content-Type: application/json' \
  -d '{
        "run_id": "ex-hc-2",
        "indices": [5, 0, 5, 1],
        "values": [[10,10],[1,1],[-10,-10],[2,2]]
      }' | python3 -m json.tool

echo "4) empty batch -> verdict empty, step not advanced (NOT reported as applied)"
curl -s -X POST "$BASE/apply" \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"ex-empty-3","indices":[],"values":[]}' | python3 -m json.tool

echo "5) out-of-range index -> HTTP 400, whole batch rejected"
curl -s -o /tmp/oor.json -w "HTTP %{http_code}\n" -X POST "$BASE/apply" \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"ex-oor-4","indices":[0,6],"values":[[1,1],[1,1]]}'
cat /tmp/oor.json | python3 -m json.tool

echo "6) inspect a hot row, a cold row, and summary"
curl -s "$BASE/state/row/0" | python3 -m json.tool
curl -s "$BASE/state/row/3" | python3 -m json.tool
curl -s "$BASE/state/summary" | python3 -m json.tool

echo "7) persist only touched rows (transactional checkpoint)"
curl -s -X POST "$BASE/checkpoint" | python3 -m json.tool
