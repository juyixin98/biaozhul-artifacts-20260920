#!/usr/bin/env bash
# End-to-end acceptance demo.
#
# Prereqs: server already running, e.g.
#   AUDIT_DATA_DIR=./demo-data AUDIT_SIGNING_KEY=./demo-keys/server.pem \
#   AUDIT_CHECKPOINT_INTERVAL=5 \
#   .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8077
#
# Usage: scripts/demo.sh [base_url]
set -u
BASE="${1:-http://127.0.0.1:8077}"
PY=.venv/bin/python
TMP=$(mktemp -d)
ANCHOR=demo-keys/server.public.pem   # obtained out-of-band by the verifier
trap 'rm -rf "$TMP"' EXIT

jget() { "$PY" -c "import json,sys; r=json.load(sys.stdin); print($1)"; }

echo "### 0. health (empty log already has a seq=0 genesis anchor)"
curl -s "$BASE/health"

echo; echo "### 1. append 3 records"
for i in 1 2 3; do
  curl -s -X POST "$BASE/records" -H 'Content-Type: application/json' \
    -d "{\"actor\":\"alice\",\"action\":\"op$i\",\"resource\":\"/r$i\",\"payload\":{\"k\":$i}}" \
    | jget "'seq='+str(r['record']['seq'])+' hash='+r['record']['hash'][:12]"
done

echo "### 2. force signed checkpoint over seq 3"
curl -s -X POST "$BASE/checkpoints" | jget "'checkpoint seq='+str(r['checkpoint']['seq'])"

echo "### 3. append two more records WITHOUT a newer checkpoint"
for i in 4 5; do
  curl -s -X POST "$BASE/records" -H 'Content-Type: application/json' \
    -d "{\"actor\":\"bob\",\"action\":\"late$i\",\"resource\":\"/x\",\"payload\":null}" >/dev/null
done
curl -s "$BASE/export" > "$TMP/undecided.json"
echo "-- verifier verdict (expect UNDETERMINED: clean unanchored tail):"
$PY -m tools.verify_export --anchor "$ANCHOR" --bundle "$TMP/undecided.json" \
  | jget "r['status']+' proven_upto='+str(r['proven_upto'])"

echo "### 4. sign seq 5 and re-verify (expect VALID)"
curl -s -X POST "$BASE/checkpoints" >/dev/null
curl -s "$BASE/export" > "$TMP/good.json"
$PY -m tools.verify_export --anchor "$ANCHOR" --bundle "$TMP/good.json" \
  | jget "r['status']+' proven_upto='+str(r['proven_upto'])"

echo "### 5. attack matrix"
attack() {
  local name="$1" expect="$2" mutation="$3"
  "$PY" - "$TMP/good.json" "$TMP/attack.json" "$mutation" <<'PYEOF'
import json, sys
src, dst, expr = sys.argv[1], sys.argv[2], sys.argv[3]
b = json.load(open(src))
exec(expr, {"b": b})
json.dump(b, open(dst, "w"))
PYEOF
  local st
  st=$($PY -m tools.verify_export --anchor "$ANCHOR" --bundle "$TMP/attack.json" \
       | jget "r['status']")
  printf '  %-42s -> %-12s (expect %s) %s\n' "$name" "$st" "$expect" \
    "$([ "$st" = "$expect" ] && echo OK || echo FAIL)"
}
attack "modify covered record"       TAMPERED  "b['records'][1]['action']='HACKED'"
attack "delete a record (gap)"       TAMPERED  "del b['records'][2]"
attack "reorder two records"         TAMPERED  "b['records'][0],b['records'][1]=b['records'][1],b['records'][0]"
attack "truncate (3/5 records)"      TRUNCATED "b['records']=b['records'][:3]"
attack "forge checkpoint signature"  TAMPERED  "b['checkpoints'][-1]['signature']='00'+b['checkpoints'][-1]['signature'][2:]"
attack "rollback tail + its anchors" UNDETERMINED "b['records']=b['records'][:4]; b['checkpoints']=b['checkpoints'][:2]"

echo "### 6. external anchor defeats silent rollback"
$PY -m tools.verify_export --anchor "$ANCHOR" --bundle "$TMP/good.json" \
  --remember "$TMP/cache.json" >/dev/null
"$PY" - "$TMP/good.json" "$TMP/rolled.json" <<'PYEOF'
import json, sys
b = json.load(open(sys.argv[1]))
b["records"] = b["records"][:3]
b["checkpoints"] = b["checkpoints"][:2]
json.dump(b, open(sys.argv[2], "w"))
PYEOF
echo "-- verifier holds cached seq=5 anchor; server serves only 3 records:"
$PY -m tools.verify_export --anchor "$ANCHOR" --bundle "$TMP/rolled.json" \
  --remember "$TMP/cache.json" | jget "r['status']+': '+r['errors'][0][:80]"

echo "### 7. bounded interior range export (start at checkpoint boundary seq 3)"
curl -s "$BASE/export?start=4&end=5" > "$TMP/slice.json"
$PY -m tools.verify_export --anchor "$ANCHOR" --bundle "$TMP/slice.json" \
  --bounded-range | jget "'slice '+str(r['slice_start'])+'..'+str(r['slice_end'])+' -> '+r['status']"
