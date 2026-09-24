#!/usr/bin/env bash
# End-to-end demo: build, start the server on an ephemeral port, exercise the
# HTTP API (doubling/split, collision 507, merge/shrink), then demonstrate
# mid-split crash recovery with crash-runner.
#
# Usage: examples/demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/target/debug/ext-hash-index"
CRASH="$ROOT/target/debug/crash-runner"
WORK="$(mktemp -d)"
PORT="${PORT:-383$(date +%S | tail -c 3)}"
trap 'kill "${SRV_PID:-0}" 2>/dev/null || true; rm -rf "$WORK"' EXIT

echo "== build =="
cargo build --manifest-path "$ROOT/Cargo.toml" >/dev/null

echo "== start server (capacity=4, u64lowbits) on 127.0.0.1:$PORT =="
"$BIN" --file "$WORK/demo.db" --addr "127.0.0.1:$PORT" \
    --capacity 4 --hash u64lowbits &
SRV_PID=$!
sleep 1
B="http://127.0.0.1:$PORT"

echo; echo "== fill one bucket (keys 0..3) then a 5th key forces a split =="
for k in 0 1 2 3 4; do
    curl -s -X PUT "$B/keys/$k" -H 'content-type: application/json' \
        -d "{\"value\":\"v$k\"}"; echo
done
echo "-- get key 4 --"; curl -s "$B/keys/4"; echo
echo "-- stats (expect global_depth 1, two buckets) --"
curl -s "$B/stats" | python3 -m json.tool

echo; echo "== delete all keys -> merges cascade, directory shrinks =="
for k in 0 1 2 3 4; do curl -s -X DELETE "$B/keys/$k" >/dev/null; done
curl -s "$B/stats" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("global_depth =",d["global_depth"]," bucket_count =",d["bucket_count"])'

kill "$SRV_PID" 2>/dev/null || true; wait "$SRV_PID" 2>/dev/null || true

echo; echo "== all-collision error (constant hash, capacity 2) =="
"$BIN" --file "$WORK/coll.db" --addr "127.0.0.1:$PORT" \
    --capacity 2 --hash constant &
SRV_PID=$!
sleep 1
for k in a b; do
    curl -s -X PUT "$B/keys/$k" -H 'content-type: application/json' \
        -d "{\"value\":\"$k\"}" -o /dev/null
done
curl -s -X PUT "$B/keys/c" -H 'content-type: application/json' \
    -d '{"value":"c"}' -w '\nHTTP %{http_code}\n'
kill "$SRV_PID" 2>/dev/null || true; wait "$SRV_PID" 2>/dev/null || true

echo; echo "== mid-split crash recovery =="
DB="$WORK/crash.db"
for k in 0 1 2 3; do
    "$CRASH" --file "$DB" --op put --key "$k" --value v \
        --capacity 4 --hash u64lowbits --crash __none__ >/dev/null
done
"$CRASH" --file "$DB" --op put --key 4 --value v4 \
    --capacity 4 --hash u64lowbits --crash split.after_directory || true
"$BIN" --file "$DB" --addr "127.0.0.1:$PORT" &
SRV_PID=$!
sleep 1
echo -n "key 4 right after reopen (must be absent): "
curl -s "$B/keys/4"; echo
curl -s -X PUT "$B/keys/4" -H 'content-type: application/json' \
    -d '{"value":"v4"}' -w '\nkey 4 now inserts, HTTP %{http_code}\n'

echo; echo "demo OK (workdir: $WORK)"
