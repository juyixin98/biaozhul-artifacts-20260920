#!/usr/bin/env bash
# End-to-end walkthrough of the reference-counting object store using curl.
#
# It builds a shared subgraph under two roots, deletes one root and runs GC to
# prove the shared blocks survive, then publishes a brand-new root *while* a GC
# is restarted, proving reachable objects are never collected. Finally it shows
# unfinished-upload retention.
#
# Usage:
#   ./examples/demo.sh            # server defaults to 127.0.0.1:8080
#   BASE=http://127.0.0.1:9000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
C=(curl -fsS)

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
jget() { python3 -c 'import sys,json;print(json.load(sys.stdin)["'"$1"'"])'; }

say "health"
"${C[@]}" "$BASE/" | python3 -m json.tool

say "upload shared data blocks b1, b2"
B1=$("${C[@]}" -X POST "$BASE/blocks" --data-binary 'shared block one' | jget hash)
B2=$("${C[@]}" -X POST "$BASE/blocks" --data-binary 'shared block two' | jget hash)
echo "b1=$B1"; echo "b2=$B2"

say "shared manifest -> {b1,b2}"
SHARED=$("${C[@]}" -X POST "$BASE/manifests" -H 'content-type: application/json' \
  -d "{\"refs\":[\"$B1\",\"$B2\"]}" | jget hash)
echo "shared=$SHARED"

say "root manifests A and B share 'shared', each with its own distinct block"
AONLY=$("${C[@]}" -X POST "$BASE/blocks" --data-binary 'a only' | jget hash)
BONLY=$("${C[@]}" -X POST "$BASE/blocks" --data-binary 'b only' | jget hash)
MA=$("${C[@]}" -X POST "$BASE/manifests" -H 'content-type: application/json' \
  -d "{\"refs\":[\"$SHARED\",\"$AONLY\"]}" | jget hash)
MB=$("${C[@]}" -X POST "$BASE/manifests" -H 'content-type: application/json' \
  -d "{\"refs\":[\"$SHARED\",\"$BONLY\"]}" | jget hash)

say "publish roots A and B"
"${C[@]}" -X PUT "$BASE/roots/A" -H 'content-type: application/json' \
  -d "{\"hash\":\"$MA\"}" >/dev/null
"${C[@]}" -X PUT "$BASE/roots/B" -H 'content-type: application/json' \
  -d "{\"hash\":\"$MB\"}" >/dev/null
"${C[@]}" "$BASE/roots" | python3 -m json.tool

say "put an unreferenced orphan block"
ORPHAN=$("${C[@]}" -X POST "$BASE/blocks" --data-binary 'nobody points at me' | jget hash)

say "delete root A, then GC (shared subgraph must survive via root B)"
"${C[@]}" -X DELETE "$BASE/roots/A" >/dev/null
"${C[@]}" -X POST "$BASE/gc" | python3 -m json.tool

say "b1,b2,shared,mB,bOnly still fetchable; mA, aOnly and orphan are gone"
for h in "$B1" "$B2" "$SHARED" "$MB" "$BONLY"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/objects/$h")
  echo "reachable $h -> HTTP $code (expect 200)"
done
for h in "$MA" "$AONLY" "$ORPHAN"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/objects/$h")
  echo "collected $h -> HTTP $code (expect 404)"
done

say "CONCURRENCY: stage root C under an upload, then GC (restart) races the publish"
# Stage the new closure under an OPEN upload so in-flight objects have an
# independent retention reason even though no root points at them yet.
UP2=$("${C[@]}" -X POST "$BASE/uploads" | jget upload_id)
C1=$("${C[@]}" -X POST "$BASE/uploads/$UP2/blocks" --data-binary 'new root block' | jget hash)
MC=$("${C[@]}" -X POST "$BASE/uploads/$UP2/manifests" -H 'content-type: application/json' \
  -d "{\"refs\":[\"$C1\"]}" | jget hash)
# Fire a GC and the publish "at the same time". Whichever order the gc_lock
# grants, the open upload (before publish) and root reachability (after) keep
# the new closure alive.
curl -fsS -X POST "$BASE/gc" >/tmp/gc1.json &
"${C[@]}" -X PUT "$BASE/roots/C" -H 'content-type: application/json' \
  -d "{\"hash\":\"$MC\"}" >/dev/null
wait
"${C[@]}" -X POST "$BASE/uploads/$UP2/complete" >/dev/null
"${C[@]}" -X POST "$BASE/gc" >/tmp/gc2.json
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/objects/$C1")
echo "new-root block $C1 -> HTTP $code (expect 200; proves no live object was collected)"

say "UNFINISHED UPLOAD retention"
UP=$("${C[@]}" -X POST "$BASE/uploads" | jget upload_id)
UB=$("${C[@]}" -X POST "$BASE/uploads/$UP/blocks" --data-binary 'half uploaded' | jget hash)
echo "open upload $UP holds $UB"
"${C[@]}" -X POST "$BASE/gc" | python3 -m json.tool
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/objects/$UB")
echo "open-upload block -> HTTP $code (expect 200 despite no root)"

say "done (note: with the default 1h retention the upload stays protected; see README)"
