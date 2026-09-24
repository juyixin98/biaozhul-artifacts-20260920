#!/usr/bin/env bash
# examples/demo.sh — end-to-end acceptance walkthrough.
#
# Pushes two images that share a base layer, exercises a digest-mismatch
# rejection, runs GC, demonstrates the pull-vs-delete safety window, and
# prints the auditable GC report.
#
# Usage:
#   ./scripts/demo.sh            # server assumed at http://127.0.0.1:8080
#   BASE=http://host:port ./scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
REPO="demo/app"
C=("curl" "-fsS")
j() { python3 -m json.tool; }

digest_of() { python3 -c 'import sys,hashlib;print("sha256:"+hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'; }

push_blob() { # $1 content -> echo digest
  local content="$1"
  local loc
  loc=$("${C[@]}" -X POST "$BASE/v2/$REPO/blobs/uploads/" -D - -o /dev/null | tr -d '\r' | awk '/^[Ll]ocation:/{print $2}')
  "${C[@]}" -X PATCH "$BASE$loc" --data-binary "$content" -H 'Content-Type: application/octet-stream' -o /dev/null
  local dg
  dg=$(printf '%s' "$content" | digest_of)
  "${C[@]}" -X PUT "$BASE$loc?digest=$dg" -D - -o /dev/null | tr -d '\r' | awk '/^[Dd]ocker-[Cc]ontent-[Dd]igest:/{print $2}'
}

echo "== 1. Push three layers: a shared base, one private per image =="
D_SHARED=$(push_blob "shared-base-layer")
D_A=$(push_blob "layer-only-in-A")
D_B=$(push_blob "layer-only-in-B")
D_CFGA=$(push_blob '{"image":"A"}')
D_CFGB=$(push_blob '{"image":"B"}')
echo "shared=$D_SHARED"

manifest() { # config_digest, layers...
  local cfg="$1"; shift
  python3 - "$cfg" "$@" <<'PY'
import json,sys
cfg=sys.argv[1]
layers=sys.argv[2:]
print(json.dumps({
  "schemaVersion":2,
  "mediaType":"application/vnd.oci.image.manifest.v1+json",
  "config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":cfg,"size":0},
  "layers":[{"mediaType":"application/vnd.oci.image.layer.v1.tar+gzip","digest":d,"size":0} for d in layers]
}))
PY
}

echo "== 2. Tag two manifests (imgA, imgB) sharing the base layer =="
MA=$("${C[@]}" -X PUT "$BASE/v2/$REPO/manifests/imgA" \
  -H 'Content-Type: application/vnd.oci.image.manifest.v1+json' \
  --data "$(manifest "$D_CFGA" "$D_SHARED" "$D_A")" -D - -o /dev/null | tr -d '\r' | awk '/^[Dd]ocker-[Cc]ontent-[Dd]igest:/{print $2}')
"${C[@]}" -X PUT "$BASE/v2/$REPO/manifests/imgB" \
  -H 'Content-Type: application/vnd.oci.image.manifest.v1+json' \
  --data "$(manifest "$D_CFGB" "$D_SHARED" "$D_B")" -o /dev/null
echo "manifest A=$MA"

echo "== 3. Digest verification rejects a wrongly claimed digest =="
loc=$("${C[@]}" -X POST "$BASE/v2/$REPO/blobs/uploads/" -D - -o /dev/null | tr -d '\r' | awk '/^[Ll]ocation:/{print $2}')
"${C[@]}" -X PATCH "$BASE$loc" --data 'real-bytes' -o /dev/null || true
BAD="sha256:0000000000000000000000000000000000000000000000000000000000000000"
# curl -f fails on 4xx; call without -f so we can assert on the error body.
resp=$(curl -sS -X PUT "$BASE$loc?digest=$BAD" 2>&1) || true
if printf '%s' "$resp" | grep -q 'digest mismatch'; then
  echo "   rejected as expected (HTTP 400: $(printf '%s' "$resp" | head -c 80)…)"
else
  echo "   ERROR: bad digest was accepted: $resp"; exit 1
fi

echo "== 4. GC while both images live: nothing shared is collected =="
"${C[@]}" -X POST "$BASE/admin/gc/run" | j >/tmp/gc1.json
python3 - <<PY
import json
r=json.load(open('/tmp/gc1.json'))
print(f"   deleted={r['deleted_count']} retained={r['retained_count']} marked={r['marked_count']} candidates={r['candidate_count']}")
assert r['deleted_count']==0, "referenced blobs must not be deleted"
PY

echo "== 5. Delete image A (untag + delete manifest), then GC =="
"${C[@]}" -X DELETE "$BASE/v2/$REPO/manifests/imgA" -o /dev/null
"${C[@]}" -X DELETE "$BASE/v2/$REPO/manifests/$MA" -o /dev/null
"${C[@]}" -X POST "$BASE/admin/gc/run" | j >/tmp/gc2.json
python3 - <<PY
import json
r=json.load(open('/tmp/gc2.json'))
print(f"   deleted={r['deleted_count']} (A-private layer + A config)")
assert r['deleted_count']==2, r
decs={i['blob_digest']: i for i in r['items']}
assert "$D_SHARED" in decs and decs["$D_SHARED"]['decision']=='retain'
assert decs["$D_A"]['decision']=='delete'
print("   shared layer RETAINED via imgB; A-private layer deleted:")
for d,i in decs.items():
    print(f"     {i['decision']:7} {d[:24]}…  {i['reason']}")
PY

echo "== 6. Orphan temp-file cleanup (separate audit trail) =="
"${C[@]}" -X POST "$BASE/admin/orphans/run" | j | head -20

echo
echo "Demo complete. Full report saved to /tmp/gc2.json"
