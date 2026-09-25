#!/usr/bin/env bash
# Request samples for the enrange HTTP service (curl).
#
# Prerequisites (one time):
#   python3 -m enrange keygen --data-dir ./data
#   python3 -m enrange serve  --data-dir ./data --host 127.0.0.1 --port 8099 &
#
# The service binds to localhost and has no accounts/tokens; keys are local
# files only.
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8099}

echo "== health =="
curl -s "$BASE/healthz"; echo

echo "== create object, server-generated id (block_size=16 for the demo) =="
printf 'XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX' > /tmp/enr-body50.bin
curl -s -D /tmp/hdr -X POST "$BASE/objects?block_size=16" \
     --data-binary @/tmp/enr-body50.bin
OID=$(curl -s -X POST "$BASE/objects?block_size=16" \
       --data-binary @/tmp/enr-body50.bin | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "created $OID"

echo "== create object with explicit id (PUT) =="
OID2=0123456789abcdef0123456789abcdef
curl -s -X PUT "$BASE/objects/$OID2?block_size=64" \
     --data-binary @/tmp/enr-body50.bin; echo

echo "== full object (200) =="
curl -s -D - "$BASE/objects/$OID" -o /tmp/enr-full.bin

echo "== first byte via HTTP Range header (206, inclusive end) =="
curl -s -D - "$BASE/objects/$OID" -H 'Range: bytes=0-0'

echo "== last byte of short final block (bytes=49-49) =="
curl -s -D - "$BASE/objects/$OID" -H 'Range: bytes=49-49'

echo "== open-ended suffix (bytes=40- => to end) =="
curl -s -D - "$BASE/objects/$OID" -H 'Range: bytes=40-'

echo "== cross-block slice via half-open query API [start,end) =="
curl -s -D - "$BASE/objects/$OID?start=14&end=20"

echo "== authenticated metadata =="
curl -s "$BASE/objects/$OID/meta"; echo
curl -s -I "$BASE/objects/$OID" | grep -iE 'content-length|x-enrange'

echo "== empty object =="
EID=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
curl -s -X PUT "$BASE/objects/$EID" --data-binary ''; echo
curl -s -D - "$BASE/objects/$EID"

echo "== list objects =="
curl -s "$BASE/objects"; echo

echo "== error cases =="
curl -s -w '\n-> %{http_code}\n' "$BASE/objects/1234567890abcdef1234567890abcdef"   # 404
curl -s -w '\n-> %{http_code}\n' "$BASE/objects/$OID?start=0&end=9999"              # 416
curl -s -w '\n-> %{http_code}\n' "$BASE/objects/$OID" -H 'Range: bytes=0-1,3-4'     # 400 or 416
rm -f /tmp/enr-body50.bin /tmp/enr-full.bin /tmp/hdr
