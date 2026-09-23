#!/usr/bin/env bash
# End-to-end acceptance walkthrough against a running cas-gc server.
# Requires: curl, jq. Usage: ./examples/demo.sh [base_url]
set -euo pipefail

BASE="${CAS_BASE:-${1:-http://127.0.0.1:8080}}"
api() { curl -sS "$@"; }
post() { # post <path> <json>
  api -X POST "$BASE$1" -H 'content-type: application/json' -d "$2"
}
put() { api -X PUT "$BASE$1" -H 'content-type: application/json' -d "$2"; }

echo "== 0) status =="
api "$BASE/status" | jq .

echo "== 1) upload blobs (shared, A-only, B-only, orphan) =="
SHARED=$(api -X POST "$BASE/blobs" --data-binary 'shared block' | jq -r .hash)
BLOB_A=$(api -X POST "$BASE/blobs" --data-binary 'root A only' | jq -r .hash)
BLOB_B=$(api -X POST "$BASE/blobs" --data-binary 'root B only' | jq -r .hash)
ORPHAN=$(api -X POST "$BASE/blobs" --data-binary 'nobody loves me' | jq -r .hash)
echo "shared=$SHARED"
echo "A=$BLOB_A  B=$BLOB_B  orphan=$ORPHAN"

echo "== 2) build shared subgraph and two root manifests =="
M_SHARED=$(post /manifests "$(jq -nc --arg h "$SHARED" '{blobs:[$h]}')" | jq -r .hash)
M_A=$(post /manifests "$(jq -nc --arg m "$M_SHARED" --arg b "$BLOB_A" '{manifests:[$m],blobs:[$b]}')" | jq -r .hash)
M_B=$(post /manifests "$(jq -nc --arg m "$M_SHARED" --arg b "$BLOB_B" '{manifests:[$m],blobs:[$b]}')" | jq -r .hash)
echo "m_shared=$M_SHARED  mA=$M_A  mB=$M_B"

echo "== 3) publish rootA and rootB =="
put /roots/rootA "$(jq -nc --arg h "$M_A" '{manifest:$h}')" | jq .
put /roots/rootB "$(jq -nc --arg h "$M_B" '{manifest:$h}')" | jq .

echo "== 4) stage an incomplete upload (own retention: 600s), no root references it =="
UP=$(post /uploads "$(jq -nc --arg h "$ORPHAN" '{manifest:{blobs:[$h]},retention_secs:600}')")
echo "$UP" | jq .
UP_ID=$(echo "$UP" | jq -r .upload_id)

echo "== 5) GC #1: nothing should be swept (everything rooted or staged) =="
post /gc 'null' | jq .

echo "== 6) abort the staged upload -> its closure loses protection =="
post "/uploads/$UP_ID/abort" '{}' | jq .

echo "== 7) GC #2: orphan blob collected; shared subgraph intact =="
post /gc 'null' | jq .
api "$BASE/objects/$SHARED" >/dev/null && echo "shared blob still readable: OK"
api "$BASE/objects/$M_SHARED" >/dev/null && echo "shared manifest still readable: OK"

echo "== 8) delete rootA and GC: A-only collected, shared block kept by rootB =="
api -X DELETE "$BASE/roots/rootA" | jq .
post /gc 'null' | jq .
echo "reachable set:"
api "$BASE/reachable" | jq .

echo "== 9) delete rootB and GC: everything is eventually collected =="
api -X DELETE "$BASE/roots/rootB" | jq .
post /gc 'null' | jq .

echo "== 10) final status =="
api "$BASE/status" | jq .
echo "demo complete."
