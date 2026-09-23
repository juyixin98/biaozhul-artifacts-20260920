#!/usr/bin/env bash
# =============================================================================
# verify_deps.sh — prove the vendored dependency is byte-identical to a pinned
# upstream commit. Dependency is fully vendored under lib/ (no network needed
# at build time); this script needs network only to RE-VERIFY the pin.
#
# Pinned: foundry-rs/forge-std @ 7239323e35487ba4339c93fe591065a63ce122aa
# (the tree `forge init` fetched; package.json inside reports 1.16.2).
# =============================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN=7239323e35487ba4339c93fe591065a63ce122aa
URL="https://github.com/foundry-rs/forge-std/archive/${PIN}.tar.gz"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "fetching $URL"
curl -fsSL "$URL" -o "$tmp/forge-std.tar.gz"
tar -xzf "$tmp/forge-std.tar.gz" -C "$tmp"

echo "comparing vendored tree against pinned upstream..."
# Compare only source-bearing paths; ignore the extracted archive metadata.
if diff -qr \
    --exclude='.git*' \
    "$ROOT/lib/forge-std" "$tmp/forge-std-${PIN}" >"$tmp/diff.txt"; then
  echo "OK: lib/forge-std is byte-identical to forge-std@${PIN}"
else
  echo "MISMATCH between vendored forge-std and ${PIN}:" >&2
  cat "$tmp/diff.txt" >&2
  exit 1
fi
