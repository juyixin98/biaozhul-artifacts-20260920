#!/usr/bin/env bash
# End-to-end demo WITHOUT touching any public registry:
# uploads a config blob, a layer blob, an image manifest and a multi-arch
# index over HTTP, then selects linux/arm64 and linux/amd64.
#
# Usage:
#   ./examples/upload_and_select.sh                # http://localhost:8080
#   BASE=http://127.0.0.1:9000 ./examples/upload_and_select.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
REPO="${REPO:-myorg/hello}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1" >&2; exit 1; }; }
need curl; need sha256sum; need python3

put_blob() {  # $1 = local file
  local file="$1" sha size
  sha=$(sha256sum "$file" | awk '{print $1}')
  size=$(wc -c < "$file" | tr -d ' ')
  curl -fsS -X PUT "$BASE/v2/$REPO/blobs/uploads/?digest=sha256:$sha" \
    --data-binary "@$file" -o /dev/null
  echo "sha256:$sha" "$size"
}

put_manifest() {  # $1 = local file, $2 = tag
  curl -fsS -X PUT "$BASE/v2/$REPO/manifests/$2" \
    -H 'content-type: application/vnd.oci.image.manifest.v1+json' \
    --data-binary "@$1"
}

echo "== health =="
curl -fsS "$BASE/healthz"; echo

# ---- arm64 image -----------------------------------------------------------
echo -n '{"os":"linux","architecture":"arm64"}' > "$WORK/arm64-config.json"
echo -n 'fake-arm64-layer-0001'      > "$WORK/arm64-layer.tar.gz"
read ARM_CFG_SHA ARM_CFG_SZ < <(put_blob "$WORK/arm64-config.json")
read ARM_LYR_SHA ARM_LYR_SZ < <(put_blob "$WORK/arm64-layer.tar.gz")

# ---- amd64 image -----------------------------------------------------------
echo -n '{"os":"linux","architecture":"amd64"}' > "$WORK/amd64-config.json"
echo -n 'fake-amd64-layer-0001'      > "$WORK/amd64-layer.tar.gz"
read AMD_CFG_SHA AMD_CFG_SZ < <(put_blob "$WORK/amd64-config.json")
read AMD_LYR_SHA AMD_LYR_SZ < <(put_blob "$WORK/amd64-layer.tar.gz")

mk_image() { # $1 out  $2 cfg sha $3 cfg size $4 layer sha $5 layer size
  python3 - "$@" <<'PY'
import json,sys
out,cfg,cfg_sz,lyr,lyr_sz = sys.argv[1:6]
doc={
 "schemaVersion":2,
 "mediaType":"application/vnd.oci.image.manifest.v1+json",
 "config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":cfg,"size":int(cfg_sz)},
 "layers":[{"mediaType":"application/vnd.oci.image.layer.v1.tar+gzip","digest":lyr,"size":int(lyr_sz)}],
}
open(out,"w").write(json.dumps(doc))
PY
}

mk_image "$WORK/arm64-img.json" "$ARM_CFG_SHA" "$ARM_CFG_SZ" "$ARM_LYR_SHA" "$ARM_LYR_SZ"
mk_image "$WORK/amd64-img.json" "$AMD_CFG_SHA" "$AMD_CFG_SZ" "$AMD_LYR_SHA" "$AMD_LYR_SZ"

ARM_IMG=$(put_manifest "$WORK/arm64-img.json" "arm64-1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["digest"])')
AMD_IMG=$(put_manifest "$WORK/amd64-img.json" "amd64-1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["digest"])')
ARM_SZ=$(curl -fsS "$BASE/v2/$REPO/manifests/$ARM_IMG" | wc -c | tr -d ' ')
AMD_SZ=$(curl -fsS "$BASE/v2/$REPO/manifests/$AMD_IMG" | wc -c | tr -d ' ')

python3 - "$WORK/index.json" "$ARM_IMG" "$ARM_SZ" "$AMD_IMG" "$AMD_SZ" <<'PY'
import json,sys
out,arm,arm_sz,amd,amd_sz=sys.argv[1:6]
doc={"schemaVersion":2,
 "mediaType":"application/vnd.oci.image.index.v1+json",
 "manifests":[
   {"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":arm,"size":int(arm_sz),
    "platform":{"architecture":"arm64","os":"linux","variant":"v8"}},
   {"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":amd,"size":int(amd_sz),
    "platform":{"architecture":"amd64","os":"linux"}},
 ]}
open(out,"w").write(json.dumps(doc,indent=2))
PY
# index upload uses the index media type
curl -fsS -X PUT "$BASE/v2/$REPO/manifests/latest" \
  -H 'content-type: application/vnd.oci.image.index.v1+json' \
  --data-binary "@$WORK/index.json" -o /dev/null
echo "== uploaded $REPO:latest (arm64 + amd64) =="

do_select() {  # $1 json body
  echo "-- request: $1"
  curl -fsS -X POST "$BASE/v2/$REPO/select" \
    -H 'content-type: application/json' -d "$1" \
  | python3 -c '
import json,sys
r=json.load(sys.stdin)["result"]
s=r["selected"]
print("  selected:", s["digest"])
print("  platform:", s["platform"])
print("  score   :", s["score"])
print("  path    :", [(h["index"], h["platform"]) for h in s["path"]])'
}

do_select '{"reference":"latest","os":"linux","architecture":"arm64"}'
do_select '{"reference":"latest","os":"linux","architecture":"amd64"}'

echo
echo "== expect NO_MATCH (ppc64le) =="
curl -s -o "$WORK/err.json" -w "  http %{http_code}\n" -X POST "$BASE/v2/$REPO/select" \
  -H 'content-type: application/json' \
  -d '{"reference":"latest","os":"linux","architecture":"ppc64le"}'
cat "$WORK/err.json"; echo
