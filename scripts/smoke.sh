#!/usr/bin/env bash
# End-to-end smoke flow against a running registry.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:18642}"
pp() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }

echo "== 1. upload five blobs (shared layer L; A,B unique; configs cfgA,cfgB)"
putblob() { # repo, file -> sets DG
  local repo=$1 file=$2 code
  DG="sha256:$(sha256sum "$file" | cut -d' ' -f1)"
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    "$BASE/v2/$repo/blobs/uploads/?digest=$DG" --data-binary @"$file")
  echo "  upload $file ($repo) -> HTTP $code  ${DG:0:19}..."
  [ "$code" = 201 ]
}
printf 'shared-layer-%s' "$RANDOM" > /tmp/L.bin
head -c 4096 /dev/urandom > /tmp/A.bin
head -c 8192 /dev/urandom > /tmp/B.bin
echo '{"cfg":"a"}' > /tmp/cfgA.json
echo '{"cfg":"b"}' > /tmp/cfgB.json
putblob alpha /tmp/L.bin; LD=$DG
putblob alpha /tmp/A.bin; AD=$DG
putblob beta  /tmp/B.bin; BD=$DG
putblob alpha /tmp/cfgA.json; CA=$DG
putblob beta  /tmp/cfgB.json; CB=$DG

echo
echo "== 2. manifests alpha:v1=[cfgA,L,A], beta:v1=[cfgB,L,B] (L shared)"
mkmanifest() { # config layer...
  local cfg=$1; shift; local layers="" l
  for l in "$@"; do layers+="{\"mediaType\":\"application/vnd.oci.image.layer.v1.tar\",\"digest\":\"$l\",\"size\":1},"; done
  layers="[${layers%,}]"
  printf '{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"%s","size":1},"layers":%s}' "$cfg" "$layers"
}
mkmanifest "$CA" "$LD" "$AD" > /tmp/ma.json
mkmanifest "$CB" "$LD" "$BD" > /tmp/mb.json
code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/vnd.oci.image.manifest.v1+json' \
  --data-binary @/tmp/ma.json "$BASE/v2/alpha/manifests/v1"); echo "  PUT alpha:v1 -> $code"; [ "$code" = 201 ]
code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H 'Content-Type: application/vnd.oci.image.manifest.v1+json' \
  --data-binary @/tmp/mb.json "$BASE/v2/beta/manifests/v1"); echo "  PUT beta:v1  -> $code"; [ "$code" = 201 ]
MAD="sha256:$(sha256sum /tmp/ma.json | cut -d' ' -f1)"
MBD="sha256:$(sha256sum /tmp/mb.json | cut -d' ' -f1)"
echo "  alpha manifest digest: ${MAD:0:19}...  beta: ${MBD:0:19}..."

echo
echo "== 3. GC with all objects live -> deletes must be 0"
curl -s -X POST "$BASE/admin/gc" -d '{}' | pp | grep -E '"(state|deleted_blobs|deleted_manifests|retained_blobs|retained_manifests)"'

echo
echo "== 4. retain reasons are audited"
curl -s "$BASE/admin/gc" | pp | grep '"id"' | head -1
RUN=$(curl -s "$BASE/admin/gc" | python3 -c 'import sys,json;print(json.load(sys.stdin)["runs"][0]["id"])')
echo "  latest run: $RUN"
curl -s "$BASE/admin/gc/$RUN" | pp | grep -A2 -F "$LD" | head -9

echo
echo "== DIGESTS $LD $AD $BD $CA $CB $MAD $MBD"
