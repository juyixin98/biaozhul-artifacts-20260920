#!/usr/bin/env bash
# echo_args.sh — prints each argv element on its own line, base64-encoded,
# so callers can assert arguments were passed verbatim with no shell
# interpretation (no expansion of $(...), globs, etc.).
set -euo pipefail
i=0
for arg in "$@"; do
  i=$((i+1))
  encoded="$(printf '%s' "$arg" | base64 -w0)"
  printf '%s\n' "$encoded"
done
