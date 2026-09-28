#!/usr/bin/env bash
# Service call examples for the STRIPS planning service.
# Assumes the server is running:  bash scripts/run_service.sh
#
# Every response includes a "run_id"; fetch the full evidence with the last
# command. Outputs are JSON - pipe through `python3 -m json.tool` for pretty.
set -euo pipefail

BASE="${STRIPS_BASE_URL:-http://127.0.0.1:8000}"
FIXTURES="$(cd "$(dirname "$0")/../fixtures" && pwd)"

echo "== 1. health =="
curl -sS "${BASE}/healthz"
echo

echo "== 2. plan on the synthetic resource domain (A* + hmax, optimal cost) =="
curl -sS -X POST "${BASE}/api/v1/plans" \
  -H "content-type: application/json" \
  --data "$(python3 -c 'import json,sys; print(json.dumps({"problem": json.load(open(sys.argv[1]))}))' "${FIXTURES}/resource_ops.json")" \
  | tee /tmp/strips_found.json
echo

echo "== 3. breadth-first: minimum ACTION count (picks the costly express plan) =="
curl -sS -X POST "${BASE}/api/v1/plans" \
  -H "content-type: application/json" \
  --data "$(python3 -c '
import json, sys
problem = json.load(open(sys.argv[1]))
print(json.dumps({"problem": problem, "options": {"algorithm": "bfs"}}))' "${FIXTURES}/resource_ops.json")"
echo

echo "== 4. provably unsolvable goal (isolated vault) =="
curl -sS -X POST "${BASE}/api/v1/plans" \
  -H "content-type: application/json" \
  --data "$(python3 -c 'import json,sys; print(json.dumps({"problem": json.load(open(sys.argv[1]))}))' "${FIXTURES}/resource_ops_unsolvable.json")"
echo

echo "== 5. resource limit -> solvability UNKNOWN, not a plan =="
curl -sS -X POST "${BASE}/api/v1/plans" \
  -H "content-type: application/json" \
  --data "$(python3 -c '
import json, sys
problem = json.load(open(sys.argv[1]))
print(json.dumps({"problem": problem, "options": {"algorithm": "astar", "heuristic": "hmax", "max_depth": 1}}))' "${FIXTURES}/resource_ops.json")"
echo

echo "== 6. input error: malformed JSON (INPUT_INVALID) =="
curl -sS -X POST "${BASE}/api/v1/plans" \
  -H "content-type: application/json" \
  --data '{not json'
echo

echo "== 7. invalid problem: unknown object reference (INVALID_PROBLEM) =="
curl -sS -X POST "${BASE}/api/v1/plans" \
  -H "content-type: application/json" \
  --data "$(python3 -c '
import json, sys
problem = json.load(open(sys.argv[1])); problem["init"].append("(at r1 mars)")
print(json.dumps({"problem": problem}))' "${FIXTURES}/resource_ops.json")"
echo

echo "== 8. independent executor: state conflict (moving into the blocked site) =="
curl -sS -X POST "${BASE}/api/v1/plans/execute" \
  -H "content-type: application/json" \
  --data "$(python3 -c '
import json, sys
print(json.dumps({"problem": json.load(open(sys.argv[1])),
                  "plan": ["(move r1 depot site1)"]}))' "${FIXTURES}/resource_ops.json")"
echo

echo "== 9. fetch evidence + replay timeline of the run from step 2 =="
RUN_ID="$(python3 -c 'import json; print(json.load(open("/tmp/strips_found.json"))["run_id"])')"
echo "run_id=${RUN_ID}"
curl -sS "${BASE}/api/v1/runs/${RUN_ID}"
echo

echo "== 10. list recent runs =="
curl -sS "${BASE}/api/v1/runs?limit=10"
echo
