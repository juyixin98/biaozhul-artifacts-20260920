#!/usr/bin/env bash
# Download + verify + vendor the single third-party dependency (Eigen 3.4.0).
# Hash is pinned in dependencies.lock. Safe to re-run; skips if present.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="https://gitlab.com/libeigen/eigen/-/archive/3.4.0/eigen-3.4.0.tar.gz"
SHA="8586084f71f9bde545ee7fa6d00288b264a2b7ac3607b974e54d13e7162c1c72"
DEST="$ROOT/third_party/eigen"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ -f "$DEST/Eigen/Dense" ]; then
  echo "[setup_deps] $DEST already present ($(grep -m1 'EIGEN_WORLD_VERSION' "$DEST/Eigen/src/Core/util/Macros.h" 2>/dev/null || echo present))."
  exit 0
fi

echo "[setup_deps] downloading Eigen 3.4.0 ..."
if command -v curl >/dev/null 2>&1; then
  curl -fSL --retry 3 -o "$TMP/eigen.tar.gz" "$URL"
else
  wget -O "$TMP/eigen.tar.gz" "$URL"
fi

GOT="$(sha256sum "$TMP/eigen.tar.gz" | awk '{print $1}')"
if [ "$GOT" != "$SHA" ]; then
  echo "[setup_deps] SHA256 MISMATCH" >&2
  echo "  expected $SHA" >&2
  echo "  got      $GOT" >&2
  exit 1
fi
echo "[setup_deps] sha256 OK"

mkdir -p "$TMP/x"
tar -xzf "$TMP/eigen.tar.gz" -C "$TMP/x" --strip-components=1
mkdir -p "$DEST"
cp -a "$TMP/x/Eigen" "$TMP/x/unsupported" "$DEST/" 2>/dev/null || cp -a "$TMP/x/Eigen" "$DEST/"
echo "[setup_deps] installed Eigen into $DEST"
