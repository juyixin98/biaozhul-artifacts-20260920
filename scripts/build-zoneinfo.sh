#!/usr/bin/env bash
# Reproducibly build the embedded IANA time zone database.
#
# The service loads zone information ONLY from internal/zoneinfo/files, so its
# DST behavior is pinned to a fixed tzdata release and does not depend on the
# host's /usr/share/zoneinfo. Re-run this script to upgrade; bump TZ_VERSION
# and TZ_SHA256 together.
set -euo pipefail

TZ_VERSION=2024a
# sha256 of https://data.iana.org/time-zones/releases/tzdata2024a.tar.gz
TZ_SHA256=0d0434459acbd2059a7a8da1f3304a84a86591f6ed69c6248fffa502b6edffe3

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/internal/zoneinfo/files"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo ">> downloading tzdata${TZ_VERSION}"
curl -fsSL "https://data.iana.org/time-zones/releases/tzdata${TZ_VERSION}.tar.gz" -o "$WORK/tzdata.tar.gz"

echo ">> verifying sha256"
echo "${TZ_SHA256}  $WORK/tzdata.tar.gz" | sha256sum -c -

tar -xzf "$WORK/tzdata.tar.gz" -C "$WORK"

echo ">> compiling zone files with zic -b fat"
STAGE="$(mktemp -d)"
trap 'rm -rf "$WORK" "$STAGE"' EXIT
# -b fat emits 64-bit TZif; the file list covers every primary zone in the
# distribution plus the backward-compatibility links. (The etcnorth source
# file was removed before the 2024a release.)
( cd "$WORK" && zic -d "$STAGE" -b fat \
    africa antarctica asia australasia europe northamerica southamerica \
    etcetera backward )

# zic materializes backward links as symbolic links, which go:embed cannot
# embed; resolve them into regular files.
rm -rf "$OUT"
mkdir -p "$OUT"
cp -rL "$STAGE/." "$OUT/"
find "$OUT" -type f | sort > "$OUT/MANIFEST"
find "$OUT" -type f | wc -l | xargs echo ">> compiled zone files:"
echo ">> done: embedded tzdata ${TZ_VERSION}"
