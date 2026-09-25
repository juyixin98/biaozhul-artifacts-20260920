#!/usr/bin/env bash
# Fixture: slow-model
#
# Writes the SAME deterministic 1 MiB artifact as "make-model.sh 1048576" but
# in 16 KiB chunks with a small delay, so a client cancelling mid-stream
# reliably observes an interrupted transfer (used by tests for the resume
# path).
set -euo pipefail

size=1048576
chunk=16384
out="model.bin"
pattern='modelcache-local-model-fixture-v0001----0123456789abcdef!!'

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
set +o pipefail
LC_ALL=C awk -v pat="$pattern" -v bs=1048576 '
  BEGIN {
    block = ""
    while (length(block) < bs) block = block pat
    while (1) printf "%s", block
  }
' | head -c "$size" > "$tmp"
set -o pipefail

: > "$out"
remaining="$size"
blockno=0
while [ "$remaining" -gt 0 ]; do
  take=$chunk
  if [ "$take" -gt "$remaining" ]; then take="$remaining"; fi
  # All chunks except possibly the last are full 16 KiB blocks.
  if [ "$take" -eq "$chunk" ]; then
    dd if="$tmp" of="$out" bs="$chunk" skip="$blockno" count=1 \
      conv=notrunc oflag=append status=none
  else
    dd if="$tmp" of="$out" bs=1 \
      skip=$((blockno * chunk)) count="$take" \
      conv=notrunc oflag=append status=none
  fi
  blockno=$((blockno + 1))
  remaining=$((remaining - take))
  sleep 0.02
done
echo "slow-model: wrote $size bytes to $out"
