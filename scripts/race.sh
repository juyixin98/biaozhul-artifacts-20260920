#!/usr/bin/env bash
# Safety-scenario checks: pull/delete race and tag-published-after-mark.
set -uo pipefail
B="${BASE:-http://127.0.0.1:18642}"
pass=0; fail=0
check() { if [ "$1" = "$2" ]; then echo "  PASS: $3 ($1)"; pass=$((pass+1)); else echo "  FAIL: $3 (want $2 got $1)"; fail=$((fail+1)); fi; }

putblob() { # file -> DG
  DG="sha256:$(sha256sum "$1" | cut -d' ' -f1)"
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/v2/alpha/blobs/uploads/?digest=$DG" --data-binary @"$1")
  [ "$code" = 201 ] || { echo "upload failed $code"; exit 1; }
}

echo "== scenario: PULL vs DELETE race"
printf 'raced-layer-%s' "$RANDOM" > /tmp/R.bin
putblob /tmp/R.bin; RD=$DG
echo "  blob $RD uploaded (unreferenced)"
# Start a pull that holds lock+lease for 3s while streaming.
curl -s -o /tmp/R.got -w '%{http_code}' -H 'X-Read-Delay: 3000' "$B/v2/alpha/blobs/$RD" >/tmp/pull.code &
PULLPID=$!
sleep 1
echo "  pull in flight (3s read delay); running GC during it..."
G=$(curl -s -X POST "$B/admin/gc" -d '{}')
DEC=$(python3 -c "import sys,json;d=json.load(sys.stdin);print(next(i['decision'] for i in d['items'] if i['digest']=='$RD'))")
REA=$(python3 -c "import sys,json;d=json.load(sys.stdin);print(next(i['reason'] for i in d['items'] if i['digest']=='$RD'))")
check "$DEC" "retain" "blob protected during active pull"
echo "    reason: $REA"
wait $PULLPID; PCODE=$(cat /tmp/pull.code)
check "$PCODE" "200" "pull completed successfully despite GC"
cmp -s /tmp/R.bin /tmp/R.got && check "integral" "integral" "pulled bytes match" || { echo "  FAIL: byte mismatch"; fail=$((fail+1)); }
# After pull finishes and lease released, GC must now collect it.
G2=$(curl -s -X POST "$B/admin/gc" -d '{}')
DEC2=$(python3 -c "import sys,json;d=json.load(sys.stdin);
items=[i['decision'] for i in d['items'] if i['digest']=='$RD'];print(items[0] if items else 'absent')")
check "$DEC2" "delete" "blob collected once pull finishes"

echo
echo "== scenario: NEW TAG after the mark snapshot"
printf 'late-layer-%s' "$RANDOM" > /tmp/Q.bin
putblob /tmp/Q.bin; QD=$DG
# Build a manifest referencing Q.
cat > /tmp/mq.json <<JSON
{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json",
 "config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"$QD","size":1},
 "layers":[{"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"$QD","size":1}]}
JSON
# GC pauses 3s between mark and sweep; publish the tag in that gap.
(curl -s -X POST "$B/admin/gc" -d '{"mark_sweep_delay":3000}' >/tmp/gc3.json) &
sleep 1
echo "  GC marked (Q unreferenced); publishing late-tag during gap..."
PCODE=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/vnd.oci.image.manifest.v1+json' \
  --data-binary @/tmp/mq.json "$B/v2/alpha/manifests/late")
check "$PCODE" "201" "late tag accepted during GC"
wait
DEC3=$(python3 -c "import sys,json;d=json.load(open('/tmp/gc3.json'));print(next(i['decision'] for i in d['items'] if i['digest']=='$QD'))")
REA3=$(python3 -c "import sys,json;d=json.load(open('/tmp/gc3.json'));print(next(i['reason'] for i in d['items'] if i['digest']=='$QD'))")
check "$DEC3" "retain" "layer referenced during scan is NOT deleted"
echo "    reason: $REA3"
curl -s -o /dev/null -w '  late tag still resolvable -> %{http_code}\n' "$B/v2/alpha/manifests/late"

echo
echo "RESULT pass=$pass fail=$fail"
[ "$fail" = 0 ]
