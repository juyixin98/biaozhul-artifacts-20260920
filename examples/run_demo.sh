#!/usr/bin/env bash
# End-to-end demo against a locally started server.
# Usage: ./examples/run_demo.sh   (start uvicorn separately, or set BASE_URL)
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
j() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

echo "== health =="
curl -s "$BASE_URL/health"; echo

echo "== create branching session =="
SID=$(curl -s -X POST "$BASE_URL/sessions" -H 'Content-Type: application/json' \
  -d '{"fixture":"branching","master_seed":1234}' | j '["session_id"]')
echo "session=$SID"

echo "== plan (counter) =="
curl -s -X POST "$BASE_URL/sessions/$SID/plan" -H 'Content-Type: application/json' \
  -d '{"rng_strategy":"counter"}' \
  | python3 -m json.tool

echo "== run + independent verification (oracle + finite differences) =="
RUN=$(curl -s -X POST "$BASE_URL/sessions/$SID/runs" -H 'Content-Type: application/json' \
  -d '{"master_seed":1234,"finite_difference":true}')
echo "$RUN" | python3 -m json.tool
echo "$RUN" | j '["verification"]["passed"]' | grep -qi true && echo "VERIFICATION PASSED"

echo "== infeasible budget (expect 507 resource_exhausted) =="
SID2=$(curl -s -X POST "$BASE_URL/sessions" -H 'Content-Type: application/json' \
  -d '{"fixture":"tight_budget"}' | j '["session_id"]')
curl -s -X POST "$BASE_URL/sessions/$SID2/plan" -H 'Content-Type: application/json' \
  -d '{"memory_budget":1}' | python3 -m json.tool
