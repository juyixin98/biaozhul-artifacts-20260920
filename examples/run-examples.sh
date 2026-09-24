#!/usr/bin/env bash
# Reproduce every example in examples/ against a locally started server.
# Usage: ./examples/run-examples.sh [PORT]
set -u
PORT="${1:-18080}"
BASE="var/demo-merges"
BIN="./target/debug/build-output-merge"

cargo build --quiet
rm -rf "$BASE"
"$BIN" --listen "127.0.0.1:$PORT" --base-dir "$BASE" >/tmp/bom-demo.log 2>&1 &
PID=$!
trap 'kill "$PID" 2>/dev/null; rm -rf "$BASE"' EXIT
sleep 0.7

for f in examples/0*.json; do
  echo "================ $f"
  curl -s -w '\n[HTTP %{http_code}]\n' -X POST "http://127.0.0.1:$PORT/merge" \
    -H 'Content-Type: application/json' --data-binary @"$f"
done

echo "================ merged tree on disk"
find "$BASE/demo-happy" -printf '%y %p  inode=%i\n' 2>/dev/null | sort -k2 || true
