#!/usr/bin/env bash
# Test fixture: compile two source slots (COMMON, PART) into one library
# artifact at $OUT_LIB. Deterministic: no timestamps, sorted/fixed ordering.
set -euo pipefail
: "${IN_COMMON:?IN_COMMON required}"
: "${IN_PART:?IN_PART required}"
: "${OUT_LIB:?OUT_LIB required}"
{
  printf '== lib begin ==\n'
  printf '%s\n' '-- common --'
  cat "$IN_COMMON"
  printf '%s\n' '-- part --'
  cat "$IN_PART"
  printf '== lib end ==\n'
} > "$OUT_LIB"
