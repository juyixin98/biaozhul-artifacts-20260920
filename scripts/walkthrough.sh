#!/usr/bin/env bash
# End-to-end demonstration against a running SynapticGo server.
#
#   bash scripts/walkthrough.sh                 # defaults to localhost:8080
#   BASE=http://localhost:18091 bash scripts/walkthrough.sh
#
# It creates a fresh user, uploads a small dataset out of order, publishes it,
# registers a 2-class linear model, runs predictions, and compares versions.
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
API="$BASE/api/v1"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

need() { command -v "$1" >/dev/null 2>&1 || { echo "need $1" >&2; exit 1; }; }
need curl
need python3

post() { curl -s -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' "$@"; }

echo "== create user =="
USER="demo_$(date +%s)"
KEY=$(curl -s -X POST "$API/users" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["api_key"])')
echo "user=$USER key=${KEY:0:10}..."

echo "== prepare dataset (out of order) =="
python3 - "$WORK" <<'PY'
import hashlib, os, sys
wd=sys.argv[1]
data=bytes((i*13+5) % 256 for i in range(100))
open(os.path.join(wd,"data.bin"),"wb").write(data)
open(os.path.join(wd,"whole"),"w").write(hashlib.sha256(data).hexdigest())
cs=30
for i in range(4):
    b=data[i*cs:(i+1)*cs]
    open(os.path.join(wd,f"chunk{i}"),"wb").write(b)
    open(os.path.join(wd,f"chunk{i}.digest"),"w").write(hashlib.sha256(b).hexdigest())
print("chunks=4 whole="+hashlib.sha256(data).hexdigest()[:16]+"...")
PY

WHOLE=$(cat "$WORK/whole")
DID=$(post -X POST "$API/datasets" -d "{\"name\":\"demo\",\"total_size\":100,\"chunk_size\":30,\"whole_digest\":\"$WHOLE\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "dataset=$DID"

for i in 3 0 1 2; do
  D=$(cat "$WORK/chunk$i.digest")
  CODE=$(curl -s -o "$WORK/r.json" -w '%{http_code}' -X PUT "$API/datasets/$DID/chunks/$i" \
    -H "Authorization: Bearer $KEY" -H "Digest: sha-256=$D" \
    --data-binary @"$WORK/chunk$i")
  echo "chunk $i -> $CODE"
done
# Idempotent retry.
D=$(cat "$WORK/chunk0.digest")
curl -s -o /dev/null -w 'chunk 0 retry -> %{http_code} (200 = idempotent)\n' -X PUT \
  "$API/datasets/$DID/chunks/0" -H "Authorization: Bearer $KEY" \
  -H "Digest: sha-256=$D" --data-binary @"$WORK/chunk0"

echo "== publish =="
post -X POST "$API/datasets/$DID/publish" \
  | python3 -c 'import sys,json;r=json.load(sys.stdin);print("status:",r["dataset"]["status"],"digest:",r["whole_digest"][:16]+"...")'

echo "== register model v1 (z0=x0-x1+0.5x2-0.25 ; z1=x1-0.5x2+0.25) =="
WB=$(python3 - <<'PY'
import struct, base64
b=struct.pack("<II",3,2)
for v in [1,-1,0.5, 0,1,-0.5]: b+=struct.pack("<f",v)
for v in [-0.25,0.25]: b+=struct.pack("<f",v)
print(base64.b64encode(b).decode())
PY
)
M1=$(post -X POST "$API/models" -d "$(python3 -c '
import json,sys
print(json.dumps({"model_name":"clf","dataset_id":int(sys.argv[1]),"input_dim":3,"classes":["cat","dog"],"weights_base64":sys.argv[2]}))' "$DID" "$WB")" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "model version id=$M1"

echo "== predictions =="
for X in '[2,1,0]' '[0,0,4]'; do
  post -X POST "$API/models/$M1/predict" -d "{\"features\":$X}" \
    | python3 -c 'import sys,json;r=json.load(sys.stdin);print("x='"$X"' ->",r["predicted_class"],"conf=%.4f"%r["confidence"])'
done

echo "== register v2 and compare (same class table) =="
M2=$(post -X POST "$API/models" -d "$(python3 -c '
import json,sys
print(json.dumps({"model_name":"clf","dataset_id":int(sys.argv[1]),"input_dim":3,"classes":["cat","dog"],"weights_base64":sys.argv[2]}))' "$DID" "$WB")" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
post "$API/experiments/compare?a=$M1&b=$M2" \
  | python3 -c 'import sys,json;c=json.load(sys.stdin);print("comparable: classes=%s runs a=%d b=%d"%(c["classes"],c["a"]["total_runs"],c["b"]["total_runs"]))'

echo
echo "WALKTHROUGH OK"
