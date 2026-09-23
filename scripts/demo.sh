#!/usr/bin/env bash
# End-to-end demo of cow-snapstore: two-level branches, interleaved
# overwrites, branch deletion, GC. Requires curl and a built binary.
set -euo pipefail

PORT="${COW_PORT:-38080}"
DATA="${COW_DATA_DIR:-/tmp/cow-demo}"
BASE="http://127.0.0.1:$PORT"
BIN="$(dirname "$0")/../target/debug/cow-snapstore"

pkill -x cow-snapstore 2>/dev/null || true
rm -rf "$DATA"
COW_DATA_DIR="$DATA" COW_PORT="$PORT" "$BIN" & SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
sleep 0.7

echo '== create main, write 4 pages =='
curl -s -X POST "$BASE/snapshots" -H 'content-type: application/json' -d '{"name":"main"}'; echo
for i in 0 1 2 3; do
  curl -s -X PUT "$BASE/snapshots/main/pages/$i" -d "main-v1-page-$i" -o /dev/null -w "put main/$i: %{http_code}\n"
done

echo '== two-level branch: b1 from main, b2 from b1 (no data copied) =='
curl -s -X POST "$BASE/snapshots" -H 'content-type: application/json' -d '{"name":"b1","from":"main"}'; echo
curl -s -X POST "$BASE/snapshots" -H 'content-type: application/json' -d '{"name":"b2","from":"b1"}'; echo
curl -s "$BASE/stats"; echo

echo '== interleaved overwrites =='
curl -s -X PUT "$BASE/snapshots/main/pages/0" -d 'main-v2-page-0' -o /dev/null -w "put main/0: %{http_code}\n"
curl -s -X PUT "$BASE/snapshots/b1/pages/1"   -d 'b1-page-1'      -o /dev/null -w "put b1/1: %{http_code}\n"
curl -s -X PUT "$BASE/snapshots/b2/pages/2"   -d 'b2-page-2'      -o /dev/null -w "put b2/2: %{http_code}\n"

echo '== b2 sees inherited pages plus its own writes =='
for i in 0 1 2 3; do echo "b2/$i: $(curl -s "$BASE/snapshots/b2/pages/$i")"; done

echo '== delete middle branch b1 =='
curl -s -X DELETE "$BASE/snapshots/b1" -o /dev/null -w "delete b1: %{http_code}\n"
curl -s "$BASE/snapshots"; echo

echo '== survivors are unaffected =='
for i in 0 1 2 3; do echo "main/$i: $(curl -s "$BASE/snapshots/main/pages/$i")"; done
curl -s "$BASE/stats"; echo

echo '== GC reclaims pages only referenced by b1 =='
curl -s -X POST "$BASE/gc"; echo
curl -s "$BASE/stats"; echo
curl -s "$BASE/snapshots/b2"; echo
