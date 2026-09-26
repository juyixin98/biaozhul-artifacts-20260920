#!/usr/bin/env bash
# Run all sample request files through utf8ctl and print the responses.
# Usage: examples/run_samples.sh [path-to-utf8ctl]
set -euo pipefail
cd "$(dirname "$0")/.."

BIN="${1:-./target/debug/utf8ctl}"
if [ ! -x "$BIN" ]; then
    echo "utf8ctl not found at $BIN -- build first: cargo build" >&2
    exit 1
fi

for f in examples/requests/*.jsonl; do
    echo "=== $f ==="
    "$BIN" < "$f"
done
