#!/bin/sh
# Explicitly supplied fixture tool. NON-deterministic on purpose: embeds a
# fresh UUID in every execution, so clean reruns produce different digests.
# Usage: concat_nondet.sh <output> <input>...
set -eu
out=$1
shift
mkdir -p "$(dirname "$out")"
{
  printf '### nondet %s\n' "$(cat /proc/sys/kernel/random/uuid)"
  for f in "$@"; do
    printf '### %s\n' "$f"
    cat "$f"
    printf '\n'
  done
} > "$out"
