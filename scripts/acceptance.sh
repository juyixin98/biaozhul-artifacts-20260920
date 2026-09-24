#!/usr/bin/env bash
# End-to-end acceptance for the OCI multi-architecture selection API.
#
# It builds the binaries, generates genuinely-hashed fixtures, starts the
# server on an ephemeral port, and asserts HTTP status codes + body invariants
# for every required scenario. Exits non-zero on the first failed assertion.
#
# Usage:  ./scripts/acceptance.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WORK="$(mktemp -d)"
trap 'set +e; [[ -n "${SRV_PID:-}" ]] && kill "$SRV_PID" 2>/dev/null; rm -rf "$WORK"' EXIT

PORT="${PORT:-8200}"
BASE="http://127.0.0.1:$PORT"
REG="$WORK/registry"
DB="$WORK/data/ocimp.db"

echo ">> build"
go build -o "$WORK/server" ./cmd/server
go build -o "$WORK/genfixtures" ./cmd/genfixtures

echo ">> generate fixtures (real SHA-256 digests/sizes, no network)"
"$WORK/genfixtures" -registry "$REG" >/dev/null

echo ">> start server on $PORT"
"$WORK/server" -addr ":$PORT" -registry "$REG" -db "$DB" >"$WORK/server.log" 2>&1 &
SRV_PID=$!

# Wait for readiness.
for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -sf "$BASE/healthz" >/dev/null

pass=0; fail=0

# assert_code METHOD PATH BODY EXPECTED_CODE [DESC]
assert_code() {
  local method="$1" path="$2" body="$3" want="$4" desc="$5"
  local got
  if [[ -n "$body" ]]; then
    got="$(curl -s -o "$WORK/resp.json" -w '%{http_code}' -X"$method" \
      -H 'Content-Type: application/json' -d "$body" "$BASE$path")"
  else
    got="$(curl -s -o "$WORK/resp.json" -w '%{http_code}' -X"$method" "$BASE$path")"
  fi
  if [[ "$got" == "$want" ]]; then
    printf '   ok   %-3s %-3s %s\n' "$got" "$want" "$desc"
    pass=$((pass+1))
  else
    printf ' FAIL  got=%s want=%s %s\n        body: %s\n' "$got" "$want" "$desc" "$(cat "$WORK/resp.json")"
    fail=$((fail+1))
  fi
}

json_get() { python3 -c 'import sys,json;d=json.load(open(sys.argv[1]));print(eval(sys.argv[2]))' "$WORK/resp.json" "$1"; }
post() { curl -s -o "$WORK/resp.json" -XPOST -H 'Content-Type: application/json' -d "$2" "$BASE$1"; }

echo ">> platform selection"
post /v1/repos/demo/two-arch/resolve '{"reference":"latest","platform":{"os":"linux","architecture":"amd64"}}'
[[ "$(json_get "d['status']")" == "succeeded" ]] && { echo '   ok   amd64 resolves'; pass=$((pass+1)); } || { echo ' FAIL  amd64'; fail=$((fail+1)); }
CHAIN_LEN="$(json_get "len(d['dependencyChain'])")"
[[ "$CHAIN_LEN" == "4" ]] && { echo '   ok   full dependency chain index,manifest,config,layer'; pass=$((pass+1)); } || { echo " FAIL  chain len $CHAIN_LEN"; fail=$((fail+1)); }

post /v1/repos/demo/two-arch/resolve '{"reference":"latest","platform":{"os":"linux","architecture":"arm64"}}'
[[ "$(json_get "d['resolved']['platform']['architecture']")" == "arm64" ]] && { echo '   ok   arm64 resolves to distinct manifest'; pass=$((pass+1)); } || { echo ' FAIL  arm64'; fail=$((fail+1)); }

echo ">> variant rules"
post /v1/repos/demo/arm-variants/resolve '{"reference":"latest","platform":{"os":"linux","architecture":"arm"}}'
[[ "$(json_get "d['resolved']['platform']['variant']")" == "v7" ]] && { echo '   ok   arm (no variant) defaults to v7'; pass=$((pass+1)); } || { echo ' FAIL  arm default'; fail=$((fail+1)); }
post /v1/repos/demo/arm-variants/resolve '{"reference":"latest","platform":{"os":"linux","architecture":"arm","variant":"v6"}}'
[[ "$(json_get "d['resolved']['platform']['variant']")" == "v6" ]] && { echo '   ok   explicit variant v6 exact-matches'; pass=$((pass+1)); } || { echo ' FAIL  arm v6'; fail=$((fail+1)); }

echo ">> ambiguity / bad config / missing layer / no match"
assert_code POST /v1/repos/demo/ambiguous/resolve '{"reference":"latest","platform":{"os":"linux","architecture":"amd64"}}' 409 "ambiguous -> 409, not a random pick"
[[ "$(json_get "d['error']['code']")" == "platform_ambiguous" ]] && { echo '   ok   error code platform_ambiguous'; pass=$((pass+1)); } || { echo ' FAIL  ambiguous code'; fail=$((fail+1)); }
assert_code POST /v1/repos/demo/bad-config/resolve '{"reference":"latest","platform":{"os":"linux","architecture":"arm64"}}' 422 "lying config platform -> 422"
assert_code POST /v1/repos/demo/missing-layer/resolve '{"reference":"latest","platform":{"os":"linux","architecture":"amd64"}}' 404 "missing layer -> 404"
assert_code POST /v1/repos/demo/two-arch/resolve '{"reference":"latest","platform":{"os":"linux","architecture":"mips"}}' 404 "unsupported platform -> 404"
assert_code POST /v1/repos/demo/two-arch/resolve '{"reference":"nope","platform":{"os":"linux","architecture":"amd64"}}' 404 "unknown tag -> 404"

echo ">> digest pinning across tag movement"
# Resolve v1 first, capture task and bound digest.
post /v1/repos/demo/tag-move/resolve '{"reference":"latest","platform":{"os":"linux","architecture":"amd64"}}'
T1="$(json_get "d['id']")"
D1="$(json_get "d['reference']['resolvedDigest']")"
# Move latest to v2.
V2="sha256:72522f9d3eec381ff8138bee9ef7ce0767f347c2a5e5326f3bd62be7195180ea"
assert_code PUT /v1/repos/demo/tag-move/tags/latest "{\"digest\":\"$V2\"}" 200 "tag latest moved"
# A new resolve follows the moved tag.
post /v1/repos/demo/tag-move/resolve '{"reference":"latest","platform":{"os":"linux","architecture":"amd64"}}'
D2="$(json_get "d['reference']['resolvedDigest']")"
[[ "$D1" != "$D2" && "$D2" == "$V2" ]] && { echo '   ok   new resolve follows moved tag'; pass=$((pass+1)); } || { echo " FAIL  new resolve $D2"; fail=$((fail+1)); }
# The old task is still pinned to the original digest and its chain head.
curl -s "$BASE/v1/repos/demo/tag-move/tasks/$T1" -o "$WORK/resp.json"
OLD_D="$(json_get "d['reference']['resolvedDigest']")"
OLD_HEAD="$(json_get "d['dependencyChain'][0]['digest']")"
[[ "$OLD_D" == "$D1" && "$OLD_HEAD" == "$D1" ]] && { echo '   ok   old task stays bound to pre-move digest+chain'; pass=$((pass+1)); } || { echo " FAIL  old task rebound $OLD_D"; fail=$((fail+1)); }

echo ">> same tag name in different repositories are different artifacts"
A="$(curl -s "$BASE/v1/repos/demo/two-arch/tags"    | python3 -c 'import sys,json;print(json.load(sys.stdin)["tags"][0]["digest"])')"
B="$(curl -s "$BASE/v1/repos/demo/ambiguous/tags"   | python3 -c 'import sys,json;print([t["digest"] for t in json.load(sys.stdin)["tags"] if t["tag"]=="latest"][0])')"
[[ -n "$A" && "$A" != "$B" ]] && { echo '   ok   identical tag names do not imply identical artifacts'; pass=$((pass+1)); } || { echo ' FAIL  same tag treated same'; fail=$((fail+1)); }

echo
echo "================= acceptance summary ================="
echo "passed: $pass   failed: $fail"
if (( fail > 0 )); then exit 1; fi
echo "ALL ACCEPTANCE CHECKS PASSED"
