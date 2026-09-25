#!/bin/sh
# Explicitly supplied fixture tool. Deterministic concatenation:
# output bytes depend only on the listed input paths and contents.
# Usage: concat.sh <output> <input>...
set -eu
out=$1
shift
mkdir -p "$(dirname "$out")"
: > "$out"
for f in "$@"; do
  printf '### %s\n' "$f" >> "$out"
  cat "$f" >> "$out"
  printf '\n' >> "$out"
done
