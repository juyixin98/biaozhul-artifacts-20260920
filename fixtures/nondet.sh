#!/usr/bin/env bash
# Test fixture: intentionally NON-deterministic build. It embeds wall-clock
# time, so every run produces different bytes even though provenance (source
# digests, tool digest, upstream records) is complete and intact. This exists
# to demonstrate that "complete provenance" != "reproducible result".
set -euo pipefail
: "${IN_SRC:?IN_SRC required}"
: "${OUT_BIN:?OUT_BIN required}"
{
  printf '== nondet begin ==\n'
  cat "$IN_SRC"
  date -u +'built-at: %Y-%m-%dT%H:%M:%S.%NZ'
  printf '== nondet end ==\n'
} > "$OUT_BIN"
