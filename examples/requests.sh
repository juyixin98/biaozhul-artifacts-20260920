#!/usr/bin/env bash
# Reproduce every selection scenario over HTTP. No external network is used;
# the server only talks to localhost and only reads local fixtures.
#
# Usage:
#   python3 scripts/gen_fixtures.py          # one-time (fixtures ship in repo)
#   cargo build --release
#   ./examples/requests.sh [BASE_URL]        # default http://127.0.0.1:18099
set -u

BASE=${1:-http://127.0.0.1:18099}
BIN=${MANIFEST_SELECTOR_BIN:-./target/release/manifest-selector}
FIXTURES=${MANIFEST_SELECTOR_FIXTURES:-./fixtures}
LISTEN=${BASE#http://}

if [[ ! -x "$BIN" ]]; then
  echo "binary $BIN not found; run: cargo build --release" >&2
  exit 2
fi

"$BIN" --fixtures "$FIXTURES" --listen "$LISTEN" >/tmp/manifest-selector-demo.log 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null || true' EXIT
sleep 1

idx() { jq -c "$1"; }
hr() { printf '\n===== %s =====\n' "$1"; }

# POST a platform body; HTTP status goes to stderr, jq-filtered body to stdout.
code_curl() { # url body jqfilter
  local tmp code
  tmp=$(mktemp)
  code=$(curl -s -o "$tmp" -w "%{http_code}" -X POST "$1" \
    -H 'content-type: application/json' -d "$2")
  printf '   [HTTP %s]\n' "$code" >&2
  jq -c "$3" "$tmp"
  rm -f "$tmp"
}

SEL="$BASE/v1/select"

hr "0) health"
curl -s "$BASE/healthz"

hr "1) linux/arm v7  (answer is array index 3, not first)"
code_curl "$SEL?reference=demo-multiarch:latest" \
  '{"os":"linux","architecture":"arm","variant":"v7"}' \
  '{digest, platform, path:[.path[].kind], via:[.path[]|.viaChildIndex]}'

hr "2) linux/arm v5"
code_curl "$SEL?reference=demo-multiarch:latest" \
  '{"os":"linux","architecture":"arm","variant":"v5"}' \
  '{platform, digest}'

hr "3) linux/arm64 v8"
code_curl "$SEL?reference=demo-multiarch:latest" \
  '{"os":"linux","architecture":"arm64","variant":"v8"}' \
  '{platform, digest}'

hr "4) linux/arm, variant omitted -> highest variant (v7)"
code_curl "$SEL?reference=demo-multiarch:latest" \
  '{"os":"linux","architecture":"arm"}' \
  '{platform}'

hr "5) missing platform s390x -> 404 no_match (examined list returned)"
code_curl "$SEL?reference=demo-multiarch:latest" \
  '{"os":"linux","architecture":"s390x"}' \
  '{code, examinedCount:(.examined|length)}'

hr "6) identical conditions, two blobs -> 409 ambiguous"
code_curl "$SEL?reference=demo-ambiguous:latest" \
  '{"os":"linux","architecture":"arm64","variant":"v8"}' \
  '{code, candidateCount:(.candidates|length), digests:[.candidates[].digest]}'

hr "7) nested index -> index/index/manifest"
code_curl "$SEL?reference=demo-nested:latest" \
  '{"os":"linux","architecture":"arm","variant":"v7"}' \
  '{digest, path:[.path[].kind]}'

hr "8) descriptor to absent blob -> 422 missing_blob (never downloads)"
code_curl "$SEL?reference=demo-missing:latest" \
  '{"os":"linux","architecture":"amd64"}' \
  '{code, digest, at}'

hr "9) tampered content -> 422 digest_mismatch (claimed vs actual)"
PAD=$(printf '11%.0s' {1..32})
LEAF=$(jq -nc --arg pad "$PAD" '{
  schemaVersion:2,
  mediaType:"application/vnd.oci.image.manifest.v1+json",
  config:{mediaType:"application/vnd.oci.image.config.v1+json",
          digest:("sha256:"+$pad), size:1},
  layers:[]}')
REAL=$(curl -s -X PUT "$BASE/admin/blobs" -H 'content-type: application/json' \
  -d "$(jq -nc --arg repo demo/tamper --argjson leaf "$LEAF" \
        '{repository:$repo, json:$leaf}')" | jq -r .digest)
CLAIMED="sha256:$(printf '22%.0s' {1..32})"
curl -s -X PUT "$BASE/admin/blobs/claim" -H 'content-type: application/json' \
  -d "$(jq -nc --arg repo demo/tamper --arg c "$CLAIMED" --argjson leaf "$LEAF" \
        '{repository:$repo, claimedDigest:$c, json:$leaf}')" \
  | idx '{verifies, claimedDigest, actualDigest}'
curl -s -X PUT "$BASE/admin/tags" -H 'content-type: application/json' \
  -d "$(jq -nc --arg r demo/tamper --arg t bad --arg d "$CLAIMED" \
        '{repository:$r,tag:$t,digest:$d}')" >/dev/null
code_curl "$SEL?reference=demo/tamper:bad" \
  '{"os":"linux","architecture":"amd64"}' \
  '{code, claimed, actual}'
echo "   (real digest for reference: $REAL)"

hr "10) tampered 3-index ring -> 422 digest_mismatch at first hop"
IDX="application/vnd.oci.image.index.v1+json"
DA="sha256:$(printf 'aa%.0s' {1..32})"
DB="sha256:$(printf 'bb%.0s' {1..32})"
DC="sha256:$(printf 'cc%.0s' {1..32})"
put_ring() { # claimedDigest targetDigest
  curl -s -X PUT "$BASE/admin/blobs/claim" -H 'content-type: application/json' \
    -d "$(jq -nc --arg repo demo/ring --arg claim "$1" --arg mt "$IDX" --arg target "$2" \
      '{repository:$repo, claimedDigest:$claim,
        json:{schemaVersion:2, mediaType:$mt,
              manifests:[{mediaType:$mt, digest:$target,
                          platform:{os:"linux",architecture:"arm64"}}]}}')" \
    | idx '{claimedDigest, verifies}'
}
put_ring "$DA" "$DB"
put_ring "$DB" "$DC"
put_ring "$DC" "$DA"
curl -s -X PUT "$BASE/admin/tags" -H 'content-type: application/json' \
  -d "$(jq -nc --arg r demo/ring --arg t latest --arg d "$DA" \
        '{repository:$r,tag:$t,digest:$d}')" >/dev/null
code_curl "$SEL?reference=demo/ring:latest" \
  '{"os":"linux","architecture":"arm64"}' \
  '{code, claimed, at}'

hr "done"
