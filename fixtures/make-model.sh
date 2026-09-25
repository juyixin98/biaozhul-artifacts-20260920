#!/usr/bin/env bash
# Fixture: make-model
#
# Writes a deterministic pseudo-model file to ./model.bin in the CURRENT
# working directory (the per-job work dir provided by buildserver). The first
# argument is the size in bytes (default 4096).
#
# The content is a fixed 64-byte pattern repeated to the requested length.
# awk streams oversized blocks and head truncates to exactly <size> bytes,
# so a multi-MiB artifact is produced in milliseconds with a stable digest.
set -euo pipefail

size="${1:-4096}"
out="model.bin"

if ! [[ "$size" =~ ^[0-9]+$ ]]; then
  echo "make-model: size must be a non-negative integer, got: $size" >&2
  exit 2
fi

pattern='modelcache-local-model-fixture-v0001----0123456789abcdef!!'

# LC_ALL=C makes awk length() byte-oriented (mawk is char-oriented under a
# UTF-8 locale). pipefail is briefly disabled because awk dies on SIGPIPE once
# head has taken exactly $size bytes — that truncation is intended.
set +o pipefail
LC_ALL=C awk -v pat="$pattern" -v bs=1048576 '
  BEGIN {
    block = ""
    while (length(block) < bs) block = block pat
    while (1) printf "%s", block
  }
' | head -c "$size" > "$out"
set -o pipefail

echo "make-model: wrote $(wc -c < "$out") bytes to $out"
