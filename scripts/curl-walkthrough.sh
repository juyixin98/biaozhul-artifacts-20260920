#!/usr/bin/env bash
# Manual HTTP walk-through. Start the server first:
#   python3 -m uvicorn app.main:app --port 8000
# Then:  bash scripts/curl-walkthrough.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000/api/v1}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== health =="
curl -fsS "$BASE/healthz"; echo

echo "== create simulation from example =="
BODY=$(python3 -c 'import json,sys;print(json.dumps({"snapshot":json.load(open(sys.argv[1]))}))' \
  "$HERE/../examples/snapshot-basic.json")
SID=$(curl -fsS -X POST "$BASE/simulations" \
  -H 'content-type: application/json' \
  --data "$BODY" | python3 -c 'import sys,json;print(json.load(sys.stdin)["simulationId"])')
echo "simulation: $SID"

echo "== create drain for node-a =="
CREATE=$(curl -fsS -i -X POST "$BASE/simulations/$SID/drains" \
  -H 'content-type: application/json' \
  -d '{"nodes":["node-a"]}')
DID=$(echo "$CREATE" | python3 -c 'import sys,json;print(json.loads(sys.stdin.read().split("\r\n\r\n",1)[1])["drainId"])')
TOKEN=$(echo "$CREATE" | grep -i '^x-plan-token:' | sed 's/^[^:]*: //I' | tr -d '\r')
echo "drain: $DID"

echo "== advance to completion (waiting up to 20 ticks) =="
curl -fsS -X POST "$BASE/simulations/$SID/drains/$DID/autorun" \
  -H 'content-type: application/json' -H "x-plan-token: $TOKEN" \
  -d '{"maxTicks":20}' | python3 -m json.tool

echo "== final PDB audit (expect violations: []) =="
curl -fsS "$BASE/simulations/$SID/audit" | python3 -m json.tool

echo "== remaining pods on node-a (expect none) =="
curl -fsS "$BASE/simulations/$SID/snapshot" \
  | python3 -c 'import sys,json;s=json.load(sys.stdin);print([p["name"] for p in s["pods"] if p["node"]=="node-a"])'
