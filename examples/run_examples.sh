#!/bin/sh
# Runs every example request against the built backend.
set -e
BIN="${1:-bin/sat_backend}"
for f in examples/*.json; do
  echo "== $f =="
  "$BIN" "$f"
  echo
done
