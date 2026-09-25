#!/usr/bin/env bash
# Test fixture: "link" an application source (APP) with a previously built
# library artifact (LIB) into a binary artifact at $OUT_BIN. Deterministic.
set -euo pipefail
: "${IN_APP:?IN_APP required}"
: "${IN_LIB:?IN_LIB required}"
: "${OUT_BIN:?OUT_BIN required}"
{
  printf '== app begin ==\n'
  printf '%s\n' '-- application --'
  cat "$IN_APP"
  printf '%s\n' '-- linked library --'
  cat "$IN_LIB"
  printf '== app end ==\n'
} > "$OUT_BIN"
