#!/usr/bin/env bash
# End-to-end demo of the cas-repo HTTP verification entry point.
# Requires: bash, curl, sha256sum, base64.
set -euo pipefail

ADDR="${ADDR:-127.0.0.1:18080}"
DATA="${DATA:-/tmp/cas-demo-data}"
BIN="${BIN:-./target/release/cas-repo}"
CURL="curl -s --max-time 10"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

say "0. start server (data dir: $DATA)"
"$BIN" serve --data "$DATA" --addr "$ADDR" &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
sleep 0.7
$CURL "http://$ADDR/health"; echo

say "1. upload two leaf blocks (raw bytes)"
LEAF1="hello from leaf one"
LEAF2="hello from leaf two"
H1=$(printf '%s' "$LEAF1" | sha256sum | cut -d' ' -f1)
H2=$(printf '%s' "$LEAF2" | sha256sum | cut -d' ' -f1)
$CURL -X POST --data-binary "$LEAF1" "http://$ADDR/blocks"; echo
$CURL -X POST --data-binary "$LEAF2" "http://$ADDR/blocks"; echo
echo "expected leaf hashes: $H1 $H2"

say "2. re-upload leaf one -> deduplicated"
$CURL -X POST --data-binary "$LEAF1" "http://$ADDR/blocks"; echo

say "3. upload a manifest referencing both leaves (json envelope)"
MANIFEST="manifest: [$H1, $H2]"
MANIFEST_B64=$(printf '%s' "$MANIFEST" | base64 -w0)
$CURL -X POST -H 'Content-Type: application/json' \
  -d "{\"data_b64\":\"$MANIFEST_B64\",\"refs\":[\"$H1\",\"$H2\"]}" \
  "http://$ADDR/blocks"; echo
MHASH=$(printf '%s' "$MANIFEST" | sha256sum | cut -d' ' -f1)
echo "manifest hash: $MHASH"

say "4. block info shows the reference list"
$CURL "http://$ADDR/blocks/$MHASH/info"; echo

say "5. publish root 'main' -> manifest (atomic pointer)"
$CURL -X PUT -H 'Content-Type: application/json' -d "{\"hash\":\"$MHASH\"}" "http://$ADDR/roots/main"; echo
$CURL "http://$ADDR/roots"; echo

say "6. download a block back and verify bytes"
$CURL "http://$ADDR/blocks/$H1"; echo

say "7. hash mismatch is rejected (declared != actual)"
$CURL -X POST -H 'Content-Type: application/json' \
  -d "{\"data_b64\":\"$(printf 'real bytes' | base64 -w0)\",\"hash\":\"$H1\"}" \
  "http://$ADDR/blocks"; echo

say "8. create an orphan block, then run GC"
ORPHAN="orphan payload $(date +%s%N)"
$CURL -X POST --data-binary "$ORPHAN" "http://$ADDR/blocks"; echo
$CURL -X POST "http://$ADDR/gc"; echo

say "9. missing reference: manifest pointing at a ghost block"
GHOST=$(printf 'never uploaded' | sha256sum | cut -d' ' -f1)
PARENT="parent-with-dangling-ref"
PARENT_B64=$(printf '%s' "$PARENT" | base64 -w0)
$CURL -X POST -H 'Content-Type: application/json' \
  -d "{\"data_b64\":\"$PARENT_B64\",\"refs\":[\"$GHOST\"]}" "http://$ADDR/blocks"; echo
PHASH=$(printf '%s' "$PARENT" | sha256sum | cut -d' ' -f1)
$CURL -X PUT -H 'Content-Type: application/json' -d "{\"hash\":\"$PHASH\"}" "http://$ADDR/roots/other"; echo
$CURL -X POST "http://$ADDR/gc"; echo
echo "(missing_references lists the ghost; the parent itself is kept)"

say "10. concurrent same-block upload (8 parallel clients)"
PAYLOAD="concurrent payload $(date +%s%N)"
CURL_PIDS=()
for i in $(seq 1 8); do
  $CURL -X POST --data-binary "$PAYLOAD" "http://$ADDR/blocks" &
  CURL_PIDS+=($!)
done
# Wait only for the curls — a bare `wait` would also wait for the server.
for pid in "${CURL_PIDS[@]}"; do wait "$pid"; done
echo; echo "(all report the same hash; exactly one deduplicated=false)"

say "11. final stats"
$CURL "http://$ADDR/stats"; echo

say "12. on-disk layout"
find "$DATA" -type f | sort

kill $SERVER_PID 2>/dev/null || true
trap - EXIT
echo
echo "demo done"
