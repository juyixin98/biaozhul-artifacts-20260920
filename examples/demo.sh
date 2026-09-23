#!/usr/bin/env bash
# End-to-end acceptance demo against a locally running server.
# Every hash/CID check is executed by the service over real bytes; this script
# only drives HTTP. Requires: npm run dev (or start) on $BASE.
set -u
BASE="${BASE:-http://127.0.0.1:3000}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/examples"

pass=0
fail=0
check() { # desc expected actual
  if [ "$2" = "$3" ]; then echo "PASS: $1 ($3)"; pass=$((pass+1));
  else echo "FAIL: $1 (expected $2, got $3)"; fail=$((fail+1)); fi
}

echo "== 0. health =="
curl -s "$BASE/health"; echo

echo; echo "== 1. upload content blocks (CID re-hashed server-side) =="
for f in put-image-v1 put-link-v1 put-metadata-v1 put-image-v2 put-metadata-v2; do
  code=$(curl -s -o /tmp/nft_put.json -w "%{http_code}" -X PUT \
    "$BASE/blocks/$(jq -r .cid "$DIR/$f.json")" \
    -H 'content-type: application/json' --data-binary "@$DIR/$f.json")
  check "PUT $f -> 201/200" "ok" "$([ "$code" = 201 ] || [ "$code" = 200 ] && echo ok || echo no:$code)"
done

echo; echo "== 2. tampered bytes must be refused (CONTENT_MISMATCH 422) =="
code=$(curl -s -o /tmp/nft_tamp.json -w "%{http_code}" -X PUT \
  "$BASE/blocks/$(jq -r .cid "$DIR/put-tampered.json")" \
  -H 'content-type: application/json' --data-binary "@$DIR/put-tampered.json")
check "tampered PUT -> 422" "422" "$code"
jq -r '.error + ": " + .message' /tmp/nft_tamp.json

echo; echo "== 3. non-UTF8 block stores (valid address) but verification fails =="
code=$(curl -s -o /tmp/nft_utf.json -w "%{http_code}" -X PUT \
  "$BASE/blocks/$(jq -r .cid "$DIR/put-non-utf8.json")" \
  -H 'content-type: application/json' --data-binary "@$DIR/put-non-utf8.json")
check "non-UTF8 PUT -> 201" "201" "$code"
code=$(curl -s -o /tmp/nft_utfv.json -w "%{http_code}" -X POST "$BASE/verify" \
  -H 'content-type: application/json' \
  -d "{\"root_cid\":\"$(jq -r .cid "$DIR/put-non-utf8.json")\"}")
check "non-UTF8 verify -> 422 INVALID_JSON" "422" "$code"
jq -r .error /tmp/nft_utfv.json

ROOT1=$(jq -r .rootCidV1 "$DIR/manifest.json")
ROOT2=$(jq -r .rootCidV2 "$DIR/manifest.json")

echo; echo "== 4. verify v1 DAG (json -> link -> media, full evidence) =="
curl -s -X POST "$BASE/verify" -H 'content-type: application/json' \
  -d "{\"root_cid\":\"$ROOT1\"}" | jq '{tokenName, version, nodesVisited, checks: .tree.checks[].id, media: .tree.children[] | select(.multicodecName=="raw") | {detected: .mediaTypeDetected, sizeMatches: .sizeMatches}}'

echo; echo "== 5. cannot register before chain genesis =="
code=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$BASE/tokens/demo-token-42" \
  -H 'content-type: application/json' -d "{\"root_cid\":\"$ROOT1\"}")
check "register pre-chain -> 400" "400" "$code"

echo; echo "== 6. build chain h0..h2; register v1 at h0; it finalizes at h2 =="
curl -s -X POST "$BASE/chain/blocks" -H 'content-type: application/json' \
  -d '{"action":"append","block_hash":"0xa100000000000001","parent_hash":null}' >/dev/null
curl -s -X PUT "$BASE/tokens/demo-token-42" -H 'content-type: application/json' \
  -d "{\"root_cid\":\"$ROOT1\"}" | jq '{version,status,confirmations}'
curl -s -X POST "$BASE/chain/blocks" -H 'content-type: application/json' \
  -d '{"action":"append","block_hash":"0xa100000000000002","parent_hash":"0xa100000000000001"}' >/dev/null
curl -s -X POST "$BASE/chain/blocks" -H 'content-type: application/json' \
  -d '{"action":"append","block_hash":"0xa100000000000003","parent_hash":"0xa100000000000002"}' >/dev/null
curl -s "$BASE/tokens/demo-token-42" | jq '{currentVersion, currentStatus, confirmations, finalized: (.confirmations>=3)}'

echo; echo "== 7. duplicate version rejected; v2 registered at h3 (unconfirmed) =="
code=$(curl -s -o /tmp/nft_dup.json -w "%{http_code}" -X PUT "$BASE/tokens/demo-token-42" \
  -H 'content-type: application/json' -d "{\"root_cid\":\"$ROOT1\"}")
check "duplicate v1 -> 409 VERSION_CONFLICT" "409" "$code"
curl -s -X POST "$BASE/chain/blocks" -H 'content-type: application/json' \
  -d '{"action":"append","block_hash":"0xa100000000000004","parent_hash":"0xa100000000000003"}' >/dev/null
curl -s -X PUT "$BASE/tokens/demo-token-42" -H 'content-type: application/json' \
  -d "{\"root_cid\":\"$ROOT2\"}" | jq '{version,status,confirmations}'

echo; echo "== 8. reorg at h3: v2 revoked, current CID returns to finalized v1 =="
curl -s -X POST "$BASE/chain/blocks" -H 'content-type: application/json' -d '{
  "action":"reorg","from_height":3,
  "new_blocks":[{"hash":"0xcc00000000000009","parent":"0xa100000000000003"}]
}' | jq '{action, newTipHeight: .newTip.height}'
echo "current token state:"; curl -s "$BASE/tokens/demo-token-42" | jq '{currentVersion,currentStatus,currentCid}'
echo "history (old CID retained):"
curl -s "$BASE/tokens/demo-token-42/history" | jq '.revisions[] | {version,status,cid:(.cid[0:18]+"…"),anchorHeight}'

echo; echo "== 9. deep reorg (4 blocks) rejected by finality rule =="
code=$(curl -s -o /tmp/nft_deep.json -w "%{http_code}" -X POST "$BASE/chain/blocks" \
  -H 'content-type: application/json' -d '{
    "action":"reorg","from_height":1,
    "new_blocks":[{"hash":"0xee00000000000001","parent":"0xa100000000000001"}]}')
check "deep reorg -> 409 DEEP_REORG_REJECTED" "409" "$code"
jq -r .message /tmp/nft_deep.json

echo; echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
