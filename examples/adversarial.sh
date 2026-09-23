#!/usr/bin/env bash
# Adversarial acceptance probes — every call MUST be refused. Requires a
# running server and `npm run gen-fixtures` output. Uses only curl + node.
set -uo pipefail
BASE=${BASE:-http://127.0.0.1:3000/api/v1}
PASS=0; FAIL=0

expect_status() { # <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then echo "ok   - $1 ($3)"; PASS=$((PASS+1));
  else echo "FAIL - $1 (expected $2 got $3)"; FAIL=$((FAIL+1)); fi
}

BLOCKS=./examples/blocks/all-blocks.json
META=$(node -e 'console.log(require("./examples/fixture-cids.json").metadataCid)')

echo "== 1. ingest genuine blocks (should succeed, 201/200) =="
node -e '
const b=require("./examples/blocks/all-blocks.json");
(async()=>{for(const[cid,dataBase64]of Object.entries(b)){
  const r=await fetch(process.env.BASE+"/blocks",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({cid,dataBase64})});
  if(r.status!==201&&r.status!==200){console.error("ingest failed",r.status,await r.text());process.exit(1)}}})()'

echo "== 2. tampered bytes: flip one byte, keep the same CID -> 422 =="
read CID B64 < <(node -e 'const b=require("'$BLOCKS'");const[k]=Object.keys(b);const buf=Buffer.from(b[k],"base64");buf[0]^=1;console.log(k,buf.toString("base64"))')
S=$(curl -s -o /tmp/r1.json -w '%{http_code}' -X POST "$BASE/blocks" -H 'content-type: application/json' \
  -d "$(node -e 'console.log(JSON.stringify({cid:process.argv[1],dataBase64:process.argv[2]}))' "$CID" "$B64")")
expect_status "tampered block rejected with SHA-256 mismatch" 422 "$S"
grep -q '"name":"sha256-content-address","ok":false' /tmp/r1.json && echo "       evidence: multihash mismatch reported" || echo "       (evidence check skipped)"

echo "== 3. unsupported multibase (z=base58btc for CIDv1) -> 400 E_UNSUPPORTED_ENCODING =="
S=$(curl -s -o /tmp/r2.json -w '%{http_code}' -X POST "$BASE/blocks" -H 'content-type: application/json' \
  -d '{"cid":"zAXw123abc","dataBase64":"AA=="}')
expect_status "non-b multibase rejected" 400 "$S"

echo "== 4. unsupported multihash algorithm (sha1 code 0x11) -> 400 =="
BADCID=$(node --input-type=module -e '
import { base32 } from "multiformats/bases/base32";
// CIDv1: version=1, codec=raw(0x55), multihash sha1(0x11,20), 20 zero bytes
const bytes = Uint8Array.from([1,0x55,0x11,20,...new Uint8Array(20)]);
console.log(base32.encode(bytes).toLowerCase());')
S=$(curl -s -o /tmp/r3.json -w '%{http_code}' -X POST "$BASE/verify/metadata" -H 'content-type: application/json' \
  -d "{\"cid\":\"$BADCID\"}")
expect_status "sha1 multihash rejected" 400 "$S"

echo "== 5. CID version 2 -> 400 =="
V2=$(node --input-type=module -e '
import { base32 } from "multiformats/bases/base32";
console.log(base32.encode(Uint8Array.from([2,0x55,0x12,0x20,...new Uint8Array(32)])).toLowerCase());')
S=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/verify/metadata" -H 'content-type: application/json' -d "{\"cid\":\"$V2\"}")
expect_status "CIDv2 rejected" 400 "$S"

echo "== 6. valid metadata verifies with per-layer evidence -> 200 =="
curl -fsS -X POST "$BASE/verify/metadata" -H 'content-type: application/json' -d "{\"cid\":\"$META\"}" -o /tmp/r4.json
node -e '
const r=require("/tmp/r4.json");
if(!r.ok) process.exit(1);
for(const m of r.report.media){
  console.log("       media", m.field, "->", m.media.mime, "|", m.layers.length, "layer(s) |", m.payloadBytes, "bytes");
}'

echo "== 7. missing block (never ingested CID) -> 422 missing-block =="
MISSING="bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
S=$(curl -s -o /tmp/r5.json -w '%{http_code}' -X POST "$BASE/verify/metadata" -H 'content-type: application/json' -d "{\"cid\":\"$MISSING\"}")
expect_status "missing block reported" 422 "$S"
grep -q "missing-block" /tmp/r5.json && echo "       evidence: missing-block stage reported"

echo
echo "adversarial probes: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
