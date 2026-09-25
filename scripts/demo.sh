#!/usr/bin/env bash
# End-to-end demonstration of the build input provenance service.
#
# It builds a shared-dependency graph, verifies transitive provenance by
# independent recomputation, queries source-change impact, contrasts complete
# provenance with reproducibility, and finally tampers with an artifact blob
# to show detection.
#
# Usage: scripts/demo.sh [base-url] [data-dir]
#   base-url default http://127.0.0.1:18791
#   data-dir default /tmp/bpdemo/data (must be the -data-dir the server runs with)
#
# Requires: a running buildprov started with the repo fixtures dir, plus curl
# and python3 (for JSON parsing). The script registers all data itself.
set -euo pipefail

BASE="${1:-http://127.0.0.1:18791}"
DATA_DIR="${2:-/tmp/bpdemo/data}"
PY=${PYTHON:-python3}

jget() { "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
post() { curl -s -w '\n%{http_code}' -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
get()  { curl -s -w '\n%{http_code}' "$BASE$1"; }

show_code() { tail -n1; }
body() { sed '$d'; }

register_source() {
  local path="$1" file="$2"
  local content
  content=$("$PY" -c 'import json,sys; print(json.dumps(open(sys.argv[1]).read()))' "$file")
  echo ">> POST /v1/sources  path=$path"
  local out
  out=$(post /v1/sources "{\"path\": \"$path\", \"content\": $content}")
  echo "   HTTP $(echo "$out" | show_code)"
}

echo "=== 1. register sources (shared: common.js; per-app: appA.js/appB.js) ==="
register_source src/common.js      testdata/sources/common.js
register_source src/appA.js        testdata/sources/appA.js
register_source src/appB.js        testdata/sources/appB.js
register_source src/nondet_src.py  testdata/sources/nondet_src.py

echo
echo "=== 2. register declared build actions (only fixture-dir scripts allowed) ==="
for t in compile_lib link_app nondet; do
  out=$(post /v1/tools "{\"name\":\"$t\",\"command\":[\"bash\",\"$t.sh\"]}")
  echo ">> tool $t -> HTTP $(echo "$out" | show_code); digest=$(echo "$out" | body | jget 'd["digest"][:19]')"
done

build() { # tool  slot=kind:ref slot=kind:ref ...  outslot
  local tool="$1"; shift
  local outslot="$1"; shift
  local inputs="[" first=1
  for b in "$@"; do
    slot="${b%%=*}"
    rest="${b#*=}"
    kind="${rest%%:*}"
    ref="${rest#*:}"
    [ $first -eq 1 ] || inputs+=","
    first=0
    if [ "$kind" = source ]; then
      inputs+="{\"slot\":\"$slot\",\"sourcePath\":\"$ref\"}"
    else
      inputs+="{\"slot\":\"$slot\",\"artifactId\":\"$ref\"}"
    fi
  done
  inputs+="]"
  post /v1/actions "{\"tool\":\"$tool\",\"inputs\":$inputs,\"outputs\":[\"$outslot\"]}"
}

echo
echo "=== 3. build the shared-dependency graph ==="
out=$(build compile_lib LIB COMMON=source:src/common.js PART=source:src/appA.js)
LIBA=$(echo "$out" | body | jget 'd["artifacts"][0]["id"]')
echo ">> libA=$LIBA  HTTP $(echo "$out" | show_code)"

out=$(build compile_lib LIB COMMON=source:src/common.js PART=source:src/appB.js)
LIBB=$(echo "$out" | body | jget 'd["artifacts"][0]["id"]')
echo ">> libB=$LIBB"

out=$(build link_app BIN APP=source:src/appA.js LIB=artifact:$LIBA)
BINA=$(echo "$out" | body | jget 'd["artifacts"][0]["id"]')
echo ">> binA=$BINA"

out=$(build link_app BIN APP=source:src/appB.js LIB=artifact:$LIBB)
BINB=$(echo "$out" | body | jget 'd["artifacts"][0]["id"]')
echo ">> binB=$BINB"

echo
echo "=== 4. independent verification: recompute every digest/signature ==="
out=$(post /v1/artifacts/$BINA/verify '{}')
echo ">> verify binA -> HTTP $(echo "$out" | show_code)"
echo "$out" | body | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print("   complete=%s depth=%s records=%s blobsRehashed=%s issues=%s" % (d["complete"], d["depth"], d["records"], d["blobsHashed"], d["issues"]))'

echo
echo "=== 5. impact of changing the SHARED source common.js ==="
out=$(get "/v1/impact?source=src/common.js")
echo "$out" | body | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print("   affected artifacts:", *d["affected"], sep="\n     - ")'

echo
echo "=== 6. complete provenance != reproducible build (time-embedding tool) ==="
out=$(build nondet BIN SRC=source:src/nondet_src.py)
NONDET=$(echo "$out" | body | jget 'd["artifacts"][0]["id"]')
echo ">> nondet artifact=$NONDET"
out=$(post /v1/artifacts/$NONDET/verify '{}')
echo "   verify.complete     = $(echo "$out" | body | jget 'd["complete"]')"
sleep 0.02
out=$(post /v1/artifacts/$NONDET/reproduce '{}')
echo "$out" | body | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print("   reproduce.reproducible =", d["reproducible"], " provenanceComplete =", d["provenanceComplete"]); [print("     slot", o["slot"], "match:", o["match"]) for o in d["outputs"]]'

echo
echo "=== 7. tamper with libA content blob, then re-verify libA/binA ==="
LIBA_DIGEST=$(get /v1/artifacts/$LIBA | body | jget 'd["digest"]')
HEX=${LIBA_DIGEST#sha256:}
BLOB=$DATA_DIR/artifacts/${HEX%"${HEX:2}"}/${HEX:2}
echo ">> overwriting blob $BLOB"
echo 'TAMPERED' > "$BLOB"
out=$(post /v1/artifacts/$LIBA/verify '{}')
echo "   verify libA HTTP $(echo "$out" | show_code):"
echo "$out" | body | "$PY" -c 'import json,sys; d=json.load(sys.stdin); [print("     ISSUE:", i["code"], "-", i["message"]) for i in (d.get("issues") or [])]'
out=$(post /v1/artifacts/$BINA/verify '{}')
echo "   verify binA (downstream consumer) HTTP $(echo "$out" | show_code):"
echo "$out" | body | "$PY" -c 'import json,sys; d=json.load(sys.stdin); [print("     ISSUE:", i["code"]) for i in (d.get("issues") or [])]'

echo
echo "=== 8. disallowed command rejected (path traversal / non-bash) ==="
out=$(post /v1/tools '{"name":"evil","command":["bash","../../../etc/passwd"]}')
echo ">> traversal tool -> HTTP $(echo "$out" | show_code) ($(echo "$out" | body | jget 'd["error"]'))"

echo
echo "demo complete. IDs: LIBA=$LIBA LIBB=$LIBB BINA=$BINA BINB=$BINB NONDET=$NONDET"
