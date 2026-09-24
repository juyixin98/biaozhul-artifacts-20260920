#!/usr/bin/env bash
# End-to-end curl demo. Starts an isolated server on a throwaway database,
# so it is safe to run repeatedly and never touches your real dispatch.db.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8070}"
BASE="http://127.0.0.1:$PORT"
TMPDB="$(mktemp -d)/demo.db"
j() { python3 -c "import sys,json;d=json.load(sys.stdin);print(json.dumps(d,indent=2,ensure_ascii=False))"; }

echo "== starting isolated server on port $PORT (db: $TMPDB) =="
DISPATCH_DB="$TMPDB" DISPATCH_SECRET="demo-secret" \
  python3 -m uvicorn app.main:app --port "$PORT" --log-level warning &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.25
done

echo "== health =="
curl -s "$BASE/health" | j

echo "== seed demo data =="
DISPATCH_DB="$TMPDB" python3 -m examples.seed_demo >/dev/null

echo "== login =="
TOKEN=$(curl -s -X POST "$BASE/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"operator","password":"demo-password-123"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
AUTH="X-Auth-Token: $TOKEN"

echo "== global minimum-cost allocation =="
OUT=$(curl -s -X POST "$BASE/dispatch/assignments/batch" -H "$AUTH")
echo "$OUT" | j
ASN=$(echo "$OUT" | python3 -c "import sys,json;print(json.load(sys.stdin)['assignments'][0]['id'])")
ATOK=$(curl -s "$BASE/assignments" -H "$AUTH" \
  | python3 -c "import sys,json;a=[x for x in json.load(sys.stdin) if x['id']=='$ASN'][0];print(a['token'])")

echo "== telemetry: measured below prediction (critical) =="
curl -s -X POST "$BASE/assignments/$ASN/telemetry" \
  -H "$AUTH" -H "X-Assignment-Token: $ATOK" -H 'Content-Type: application/json' \
  -d '{"measured_soc_kwh": 1.0, "note": "field measurement"}' | j

echo "== cancel releases battery + charger pre-emption =="
curl -s -X POST "$BASE/assignments/$ASN/cancel" -H "$AUTH" | j

echo "== re-allocate after release =="
curl -s -X POST "$BASE/dispatch/assignments/batch" -H "$AUTH" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('assignments:',[(a['robot_id'],a['task_id'],a['charger_id']) for a in d['assignments']])"

echo "== charger failure recompute =="
curl -s -X POST "$BASE/chargers/C1/failover" -H "$AUTH" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('released:',len(d['released_reservations']),'reassigned:',len(d['reassignments']),'risk_alerts:',len(d['risk_alerts']))"

echo "== alerts =="
curl -s "$BASE/alerts" | j
