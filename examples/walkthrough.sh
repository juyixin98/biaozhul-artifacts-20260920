#!/usr/bin/env bash
# End-to-end walkthrough of the SynapticGo API using only curl + coreutils.
#
# It registers a user, uploads examples/dataset.json in randomly ordered
# chunks, publishes it, registers the example linear-softmax model bound to
# that dataset, runs a real inference, logs/queries experiments and finally
# exercises version comparison.
#
# Usage:
#   ./examples/walkthrough.sh [base_url]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
CHUNK_SIZE=40
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

j() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(eval(sys.argv[1]))' "$1"; }
req() { curl -sS -o "$TMP/out" -w '%{http_code}' "$@"; }

echo ">> register user"
code=$(req -X POST "$BASE/v1/users" -H 'Content-Type: application/json' \
  -d "{\"name\":\"demo-$RANDOM\"}")
[ "$code" = 201 ] || { echo "register failed $code"; cat "$TMP/out"; exit 1; }
TOKEN=$(j "d['token']" < "$TMP/out")
AUTH="Authorization: Bearer $TOKEN"
echo "   token ${TOKEN:0:12}..."

echo ">> split dataset into ${CHUNK_SIZE}-byte chunks"
IN="examples/dataset.json"
[ -f "$IN" ] || IN="$(dirname "$0")/dataset.json"
TOTAL=$(wc -c < "$IN")
WHOLE=$(sha256sum "$IN" | cut -d' ' -f1)
split -b "$CHUNK_SIZE" -d "$IN" "$TMP/p-"
MANIFEST="$TMP/manifest.json"
: > "$TMP/specs"
idx=0
: > "$MANIFEST.tmp"
printf '[' > "$MANIFEST"
first=1
for f in "$TMP"/p-*; do
  size=$(wc -c < "$f")
  sha=$(sha256sum "$f" | cut -d' ' -f1)
  [ "$first" = 1 ] || printf ',' >> "$MANIFEST"
  printf '{"idx":%d,"size":%d,"sha256":"%s"}' "$idx" "$size" "$sha" >> "$MANIFEST"
  first=0
  idx=$((idx+1))
done
printf ']' >> "$MANIFEST"

echo ">> create upload session (total=$TOTAL bytes, $idx chunks)"
CREATE=$(cat <<EOF
{"name":"demo-data","total_size":$TOTAL,"chunk_size":$CHUNK_SIZE,"whole_sha256":"$WHOLE","chunks":$(cat "$MANIFEST")}
EOF
)
code=$(req -X POST "$BASE/v1/datasets" -H 'Content-Type: application/json' \
  -H "$AUTH" -d "$CREATE")
[ "$code" = 201 ] || { echo "create failed $code"; cat "$TMP/out"; exit 1; }
DSID=$(j "d['id']" < "$TMP/out")
echo "   dataset id=$DSID"

echo ">> upload chunks in shuffled order"
mapfile -t files < <(printf '%s\n' "$TMP"/p-* | shuf)
i=0
for f in "${files[@]}"; do
  # recover original index from filename suffix
  n=$(basename "$f" | sed 's/^p-0*//')
  [ -z "$n" ] && n=0
  code=$(req -X PUT "$BASE/v1/datasets/$DSID/chunks/$n" \
    -H 'Content-Type: application/octet-stream' -H "$AUTH" --data-binary "@$f")
  [ "$code" = 200 ] || { echo "chunk $n failed $code"; cat "$TMP/out"; exit 1; }
  i=$((i+1))
done
echo "   uploaded $i chunks out of order"

echo ">> publish"
code=$(req -X POST "$BASE/v1/datasets/$DSID/publish" -H "$AUTH")
[ "$code" = 200 ] || { echo "publish failed $code"; cat "$TMP/out"; exit 1; }
j "d['status']" < "$TMP/out" | xargs echo "   status:"

echo ">> idempotent identical chunk retransmit"
first_chunk=$(printf '%s\n' "$TMP"/p-* | head -1)
code=$(req -X PUT "$BASE/v1/datasets/$DSID/chunks/0" \
  -H 'Content-Type: application/octet-stream' -H "$AUTH" --data-binary "@$first_chunk")
echo "   retransmit status=$code (200 = idempotent)"

echo ">> register model bound to dataset $DSID"
WEIGHTS="$(dirname "$0")/weights.json"
[ -f "$WEIGHTS" ] || WEIGHTS=examples/weights.json
BODY=$(python3 -c "import json;d=json.load(open('$WEIGHTS'));d['train_dataset_id']=$DSID;print(json.dumps(d))")
code=$(req -X POST "$BASE/v1/models" -H 'Content-Type: application/json' \
  -H "$AUTH" -d "$BODY")
[ "$code" = 201 ] || { echo "register model failed $code"; cat "$TMP/out"; exit 1; }
MID=$(j "d['id']" < "$TMP/out")
echo "   model version id=$MID"

echo ">> run inference for x=[1,1]"
code=$(req -X POST "$BASE/v1/models/$MID/predict" -H 'Content-Type: application/json' \
  -H "$AUTH" -d '{"x":[1,1]}')
[ "$code" = 200 ] || { echo "predict failed $code"; cat "$TMP/out"; exit 1; }
cat "$TMP/out"; echo

echo ">> experiments recorded for this model"
req "$BASE/v1/experiments?model_version_id=$MID" -H "$AUTH"
cat "$TMP/out"; echo

echo ">> register second version and compare metrics"
BODY2=$(python3 -c "import json;d=json.load(open('$WEIGHTS'));d['version']='v2';d['metrics']={'accuracy':0.5};print(json.dumps(d))")
code=$(req -X POST "$BASE/v1/models" -H 'Content-Type: application/json' \
  -H "$AUTH" -d "$BODY2")
[ "$code" = 201 ] || { echo "register v2 failed $code"; cat "$TMP/out"; exit 1; }
MID2=$(j "d['id']" < "$TMP/out")
req "$BASE/v1/model-comparisons?a=$MID&b=$MID2" -H "$AUTH"
cat "$TMP/out"; echo
echo ">> done"
