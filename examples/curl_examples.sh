#!/usr/bin/env bash
# Service call examples. Start the server first:
#   python -m strips_planner.service
set -euo pipefail

BASE="${PLANNER_BASE:-http://127.0.0.1:8000}"

echo "1) health"
curl -s "$BASE/health" | python -m json.tool

echo "2) plan a solvable problem (full fixture domain)"
python - <<'PY'
import json, urllib.request

domain = json.load(open("fixtures/domain_resource_ops.json"))
problem = json.load(open("fixtures/problem_cost_paths.json"))
body = json.dumps({
    "domain": domain,
    "problem": problem,
    "options": {"algorithm": "ucs", "heuristic": "zero"},
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8000/plan", data=body,
    headers={"content-type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    payload = json.load(resp)
print("run_id:", payload["run_id"])
print("verdict:", payload["verdict"], "cost:", payload["cost"])
print("plan:", payload["plan"])
open("examples/_last_run_id", "w").write(payload["run_id"])
PY

RUN_ID="$(cat examples/_last_run_id)"
echo "3) fetch independently verified steps for run $RUN_ID"
curl -s "$BASE/runs/$RUN_ID/steps" | python -m json.tool

echo "4) replay the stored request under a new run id"
curl -s -X POST "$BASE/runs/$RUN_ID/replay" | python -m json.tool

echo "5) input error category (422) - missing action name"
curl -s -o /tmp/err.json -w "%{http_code}\n" -X POST "$BASE/plan" \
  -H "content-type: application/json" \
  -d '{"domain":{"name":"d","actions":[{"parameters":["?x"]}]},"problem":{"name":"p","objects":["o"],"goal":{"pos":["q(o)"],"neg":[]}}}'
python -m json.tool /tmp/err.json

echo "6) unknown verdict (200) - search bound hit"
curl -s -X POST "$BASE/plan" -H "content-type: application/json" \
  -d @<(python -c 'import json;d=json.load(open("fixtures/domain_resource_ops.json"));p=json.load(open("fixtures/problem_unsolvable_missing.json"));print(json.dumps({"domain":d,"problem":p,"options":{"max_expansions":5}}))') \
  | python -c 'import sys,json;r=json.load(sys.stdin);print(r["verdict"], r["reason"], r["run_id"])'
