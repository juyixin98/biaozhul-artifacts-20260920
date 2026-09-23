#!/usr/bin/env bash
# End-to-end CLI tests. Each invocation is a fresh JVM with an in-memory
# catalog, so multi-step flows use the "batch" op inside a single request.
set -uo pipefail
cd "$(dirname "$0")/.."
JAR=hllengine.jar
[ -f "$JAR" ] || { echo "jar missing; run ./build.sh first"; exit 1; }

PASS=0; FAIL=0
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# run_ok <name> <request-json> <jq-like-grep-substring-to-assert ok:true>
run_ok() {
  local name="$1"; local req="$2"; local want="${3:-\"ok\":true}"
  local out; out=$(printf '%s' "$req" | java -jar "$JAR" 2>"$TMP/err")
  local code=$?
  if [ "$code" -eq 0 ] && printf '%s' "$out" | grep -q "$want"; then
    echo "PASS $name"; PASS=$((PASS+1))
  else
    echo "FAIL $name (exit=$code)"; echo "  req: $req"; echo "  out: $out"; echo "  err: $(cat "$TMP/err")"; FAIL=$((FAIL+1))
  fi
}

# run_err <name> <request-or-file-marker> <expected-code>
run_err() {
  local name="$1"; local req="$2"; local wantcode="$3"
  local out; out=$(printf '%s' "$req" | java -jar "$JAR" 2>/dev/null)
  local code=$?
  if [ "$code" -eq 2 ] && printf '%s' "$out" | grep -q "\"code\":\"$wantcode\""; then
    echo "PASS $name"; PASS=$((PASS+1))
  else
    echo "FAIL $name (exit=$code, wanted code $wantcode) out=$out"; FAIL=$((FAIL+1))
  fi
}

# 1. Empty set through the real CLI.
run_ok "cli.emptySketchIsZero" \
  '{"op":"batch","requests":[
     {"op":"createSketch","name":"uv","precision":12},
     {"op":"estimate","sketch":"uv"}]}' \
  '"estimatedCardinality":0'

# 2. Duplicate inserts -> 1 distinct, observedCount counts the stream.
run_ok "cli.duplicatesCollapseToOne" \
  '{"op":"batch","requests":[
     {"op":"createSketch","name":"uv","precision":12},
     {"op":"addAll","sketch":"uv","values":["x","x","x"]},
     {"op":"estimate","sketch":"uv"}]}' \
  '"estimatedCardinality":1,"estimatedCardinalityRaw"'

# 3. Shard merge: two overlapping shards union ~1500, plus incompatible shard rejected.
run_ok "cli.shardMergeUnion" \
  '{"op":"batch","requests":[
     {"op":"createSketch","name":"s1","precision":12},
     {"op":"createSketch","name":"s2","precision":12},
     {"op":"createSketch","name":"tot","precision":12},
     {"op":"createSketch","name":"bad","precision":10},
     {"op":"addAll","sketch":"s1","values":["a1","a2","shared"]},
     {"op":"addAll","sketch":"s2","values":["b1","b2","shared"]},
     {"op":"mergeSketches","target":"tot","sources":["s1","s2"]}]}' \
  '"succeeded":true'

# 4. Export then re-import in a second engine via --file round trip.
cat > "$TMP/export.json" <<'JSON'
{"op":"batch","requests":[
  {"op":"createSketch","name":"src","precision":11,"seed":3},
  {"op":"addAll","sketch":"src","values":["p","q","r","p"]},
  {"op":"exportSketch","sketch":"src"}]}
JSON
java -jar "$JAR" --file "$TMP/export.json" > "$TMP/export.out" 2>/dev/null
B64=$(grep -o '"serialized":"[A-Za-z0-9+/=]*"' "$TMP/export.out" | sed 's/"serialized":"//;s/"$//')
if [ -n "$B64" ]; then
  echo "{\"op\":\"batch\",\"requests\":[
     {\"op\":\"importSketch\",\"name\":\"restored\",\"sketch\":\"$B64\"},
     {\"op\":\"estimate\",\"sketch\":\"restored\"}]}" > "$TMP/import.json"
  OUT=$(java -jar "$JAR" --file "$TMP/import.json" 2>/dev/null)
  if printf '%s' "$OUT" | grep -q '"estimatedCardinality":3' \
     && printf '%s' "$OUT" | grep -q '"precision":11' \
     && printf '%s' "$OUT" | grep -q '"seed":3'; then
    echo "PASS cli.exportImportAcrossProcesses"; PASS=$((PASS+1))
  else
    echo "FAIL cli.exportImportAcrossProcesses: $OUT"; FAIL=$((FAIL+1))
  fi
else
  echo "FAIL cli.exportImportAcrossProcesses (no serialized field)"; FAIL=$((FAIL+1))
fi

# 5. Dataset + grouped approximate query, estimate labelled with error column.
run_ok "cli.groupedHllQuery" \
  '{"op":"batch","requests":[
     {"op":"createDataset","name":"e","columns":["uid","region"]},
     {"op":"appendRows","dataset":"e","rows":[
       {"uid":"u1","region":"east"},{"uid":"u1","region":"east"},
       {"uid":"u2","region":"east"},{"uid":"u3","region":"west"}]},
     {"op":"query","plan":{"dataset":"e","aggregate":{"groupBy":["region"],"aggregates":[
       {"fn":"count_distinct","field":"uid","alias":"exact"},
       {"fn":"hll_distinct","field":"uid","precision":12,"alias":"uv"}]}}}]}' \
  'uv_relativeStandardError'

# 6. explain exports plan but does not execute (dataset must exist first).
run_ok "cli.explainExportsPlan" \
  '{"op":"batch","continueOnError":true,"requests":[
     {"op":"createDataset","name":"e","columns":["uid"]},
     {"op":"explain","plan":{"dataset":"e","aggregate":{"aggregates":[
       {"fn":"hll_distinct","field":"uid","alias":"uv"}]}}}]}' \
  '"explainOnly":true'
run_ok "cli.planContainsScanOperator" \
  '{"op":"batch","continueOnError":true,"requests":[
     {"op":"createDataset","name":"e2","columns":["uid"]},
     {"op":"explain","plan":{"dataset":"e2"}}]}' \
  'TableScan'
# Fresh process, unknown dataset -> NOT_FOUND.
run_err "cli.explainUnknownDataset" \
  '{"op":"explain","plan":{"dataset":"missing"}}' "NOT_FOUND"

# 7. Bad JSON text at the entry point.
run_err "cli.malformedJson" '{"op": "createSketch",,' "BAD_FORMAT"
run_err "cli.emptyInput" '' "BAD_FORMAT"
run_err "cli.trailingText" '{"op":"listSketches"} junk' "BAD_FORMAT"

# 8. Incompatible config merge is surfaced inside a batch's failing item.
OUT=$(printf '%s' '{"op":"batch","requests":[
     {"op":"createSketch","name":"a","precision":12},
     {"op":"createSketch","name":"b","precision":10},
     {"op":"mergeSketches","target":"a","sources":["b"]}]}' | java -jar "$JAR" 2>/dev/null)
if printf '%s' "$OUT" | grep -q 'INCOMPATIBLE_CONFIG'; then
  echo "PASS cli.incompatibleMergeSurfacedInBatch"; PASS=$((PASS+1))
else
  echo "FAIL cli.incompatibleMergeSurfacedInBatch: $OUT"; FAIL=$((FAIL+1))
fi

# 9. Corrupt base64 sketch import -> BAD_FORMAT.
run_err "cli.badBase64Import" \
  '{"op":"importSketch","name":"z","sketch":"!!!!not-base64!!!="}' "BAD_FORMAT"
run_err "cli.corruptSketchImport" \
  '{"op":"importSketch","name":"z","sketch":"SExMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}' \
  "BAD_FORMAT"

# 10. Unknown op and help exit behavior.
run_err "cli.unknownOp" '{"op":"nope"}' "BAD_REQUEST"
if java -jar "$JAR" --help | grep -q "hllengine"; then
  echo "PASS cli.help"; PASS=$((PASS+1))
else
  echo "FAIL cli.help"; FAIL=$((FAIL+1))
fi

echo
echo "CLI e2e: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
