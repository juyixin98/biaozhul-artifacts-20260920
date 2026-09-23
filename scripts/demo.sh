#!/usr/bin/env bash
# Acceptance demo: builds the three required shard classes (all-NULL,
# boundary-equality, missing statistics), runs pruned vs FULL_SCAN queries,
# reports matched rows, aggregates and bytes read.
#
# Usage:
#   scripts/demo.sh            # start an ephemeral server, run, stop it
#   scripts/demo.sh --keep     # leave the server running
#   PORT=9090 scripts/demo.sh  # choose a port (default 18700)
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-18700}"
BASE="http://localhost:${PORT}"
DATA="$(pwd)/data-demo"
LOG="/tmp/colscan-demo.log"
[ -d out ] || ./build.sh >/dev/null

need() { command -v "$1" >/dev/null || { echo "missing dependency: $1" >&2; exit 1; }; }
need curl; need jq

if [ -n "${JAVA_HOME:-}" ]; then JAVA="$JAVA_HOME/bin/java"; else JAVA="java"; fi

post() { # post <path> <json-file>
  curl -sS -X POST "$BASE$1" -H 'Content-Type: application/json' --data-binary @"$2"
}

started_here=0
SRV_PID=""
cleanup() {
  if [ "$started_here" = "1" ] && [ "${KEEP:-0}" != "1" ] && [ -n "$SRV_PID" ]; then
    echo; echo "stopping demo server (pid $SRV_PID) ..."
    kill "$SRV_PID" 2>/dev/null || true
    wait "$SRV_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [ "${1:-}" = "--keep" ]; then KEEP=1; else KEEP=0; fi

if curl -sS --max-time 1 "$BASE/health" >/dev/null 2>&1; then
  echo "using already-running server at $BASE"
else
  rm -rf "$DATA"
  : > "$LOG"
  "$JAVA" -cp out colscan.Server --port "$PORT" --data "$DATA" >>"$LOG" 2>&1 &
  SRV_PID=$!
  started_here=1
  for _ in $(seq 1 50); do
    if curl -sS --max-time 1 "$BASE/health" >/dev/null 2>&1; then break; fi
    if ! kill -0 "$SRV_PID" 2>/dev/null; then
      echo "server failed to start; log:"; cat "$LOG"; exit 1
    fi
    sleep 0.1
  done
  echo "demo server started on $BASE (pid $SRV_PID, data $DATA, log $LOG)"
fi

curl -sS -X DELETE "$BASE/tables/sales" >/dev/null || true

echo
echo "== ingest: 5 shards =="
echo "  s1-all-null      : price/qty ALL NULL"
echo "  s2-price-10      : price = 10,10,10 (boundary)"
echo "  s3-price-10-20   : price = 10,15,20 (boundary, one NULL qty)"
echo "  s4-price-20-30   : price = 20,25,30 (boundary)"
echo "  s5-no-stats      : price = 99,100 but stats MISSING -> always scanned"

post /tables/sales/shards/s1-all-null    examples/ingest-s1-all-null.json       | jq '{shard:.ingested, stats}'
post /tables/sales/shards/s2-price-10    examples/ingest-s2-price-10.json       | jq '{shard:.ingested, stats}'
post /tables/sales/shards/s3-price-10-20 examples/ingest-s3-price-10-20.json   | jq '{shard:.ingested, stats}'
post /tables/sales/shards/s4-price-20-30 examples/ingest-s4-price-20-30.json   | jq '{shard:.ingested, stats}'
post /tables/sales/shards/s5-no-stats    examples/ingest-s5-no-stats.json      | jq '{shard:.ingested, stats}'

run_verify() { # run_verify <title> <request-file>
  echo
  echo "== $1 =="
  echo "-- request ($2) --"; jq . "$2"
  echo "-- /verify (pruned vs full scan) --"
  curl -sS -X POST "$BASE/verify" -H 'Content-Type: application/json' --data-binary @"$2" \
    | jq '{correct, differences,
           pruned:{mode:.pruned.mode, scanned:.pruned.scannedShardIds,
                   pruned:[.pruned.prunedShards[].shardId],
                   pruneReasons:.pruned.prunedShards,
                   matchedRowsCount:.pruned.matchedRowsCount,
                   aggregates:.pruned.aggregates,
                   bytesRead:.pruned.io.bytesRead,
                   metadataBytes:.pruned.io.metadataBytes,
                   dataBytes:.pruned.io.dataBytes},
           fullScan:{scanned:.fullScan.scannedShardIds,
                     matchedRowsCount:.fullScan.matchedRowsCount,
                     aggregates:.fullScan.aggregates,
                     bytesRead:.fullScan.io.bytesRead},
           bytesSaved, bytesReadRatio, shardsSkipped}'
}

run_verify 'Q1: price = 20 (boundary equality; NULL never equals 20)' examples/q1-eq-20.json
run_verify 'Q2: price >= 30 (inclusive boundary; missing-stats shard still scanned)' examples/q2-ge-30.json
run_verify 'Q3: price IS NULL (only the all-NULL shard can hold NULLs)' examples/q3-is-null.json
run_verify 'Q4: price = 15 AND qty IS NULL (NULL qty must not count as 0)' examples/q4-null-qty.json
run_verify 'Q5: 15 <= price <= 25 with rows projected' examples/q5-range-rows.json

echo
echo "== Q6: forced FULL_SCAN of price = 20 (reference numbers) =="
jq '.mode="FULL_SCAN"' examples/q1-eq-20.json \
  | curl -sS -X POST "$BASE/query" -H 'Content-Type: application/json' --data-binary @- \
  | jq '{mode, shardsTotal, shardsScanned, matchedRowsCount,
         bytesRead:.io.bytesRead, dataBytes:.io.dataBytes, metadataBytes:.io.metadataBytes}'

echo
echo "demo complete."
if [ "$KEEP" = "1" ]; then
  echo "server left running at $BASE (data $DATA)"
fi
exit 0
