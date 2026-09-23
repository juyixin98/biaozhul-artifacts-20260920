#!/usr/bin/env bash
# Manual acceptance walkthrough over the local HTTP entry point.
# Requires a running server:  ./target/release/mvcc-server --dir /tmp/mvcc-demo
# Requires curl (jq optional, only used for pretty printing).
set -u
BASE="${BASE:-http://127.0.0.1:18080}"
pp() { if command -v jq >/dev/null; then jq -c . ; else cat; fi; }
req() { # METHOD PATH [BODY]
  local m="$1" p="$2" b="${3:-}"
  echo "-- $m $p ${b:+  body: $b}"
  if [ -n "$b" ]; then
    curl -s -X "$m" "$BASE$p" -H 'Content-Type: application/json' -d "$b" -w '   [HTTP %{http_code}]\n' | { pp 2>/dev/null || cat; }
  else
    curl -s -X "$m" "$BASE$p" -w '   [HTTP %{http_code}]\n' | { pp 2>/dev/null || cat; }
  fi
}

echo '### seed v1: x=v1, y=y1'
W0=$(curl -s -X POST "$BASE/tx/write" -d '{}' | jq -r .write_txn_id)
req PUT  "/tx/write/$W0" '{"ops":[{"put":{"k":"x","v":"v1"}},{"put":{"k":"y","v":"y1"}}]}'
req POST "/tx/write/$W0/commit" '{}'

echo; echo '### long reader pins v1'
R=$(curl -s -X POST "$BASE/tx/read" -d '{}' | jq -r .read_txn_id)
echo "opened read txn id=$R on version 1"
req GET  "/tx/read/$R/get?key=x"

echo; echo '### writers A and B both start on snapshot v1, both touch x'
WA=$(curl -s -X POST "$BASE/tx/write" -d '{}' | jq -r .write_txn_id)
WB=$(curl -s -X POST "$BASE/tx/write" -d '{}' | jq -r .write_txn_id)
req PUT  "/tx/write/$WA" '{"ops":[{"put":{"k":"x","v":"A"}}]}'
req PUT  "/tx/write/$WB" '{"ops":[{"put":{"k":"x","v":"B"}}]}'
req POST "/tx/write/$WA/commit" '{}'
req POST "/tx/write/$WB/commit" '{}'   # expect 409 conflict

echo; echo '### latest read shows A; the long reader still sees v1'
req GET  "/get?key=x"
req GET  "/tx/read/$R/get?key=x"
req GET  "/tx/read/$R/get?key=y"

echo; echo '### add history v4..v8'
for i in 3 4 5 6 7; do
  W=$(curl -s -X POST "$BASE/tx/write" -d '{}' | jq -r .write_txn_id)
  curl -s -X PUT "$BASE/tx/write/$W" -d "{\"ops\":[{\"put\":{\"k\":\"x\",\"v\":\"n$i\"}}]}" >/dev/null
  curl -s -X POST "$BASE/tx/write/$W/commit" -d '{}' | jq -c '{committed,version}'
done

echo; echo '### stats with the reader active (watermark pinned to 1)'
req GET "/stats"
echo; echo '### GC while reader active, then reader re-check'
req POST "/gc" '{}'
req GET  "/tx/read/$R/get?key=x"

echo; echo '### release reader; watermark moves; GC now reclaims'
req POST "/tx/read/$R/release" '{}'
req GET  "/stats"
req POST "/gc" '{}'
req GET  "/get?key=x"
req GET  "/stats"
