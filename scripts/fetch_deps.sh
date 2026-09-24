#!/usr/bin/env bash
# Fetch and verify vendored dependencies. Re-run from a clean checkout.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS="$ROOT/deps"
THIRD="$ROOT/third_party"
mkdir -p "$DEPS" "$THIRD"

EIGEN_URL="https://gitlab.com/libeigen/eigen/-/archive/3.4.0/eigen-3.4.0.tar.gz"
EIGEN_SHA="8586084f71f9bde545ee7fa6d00288b264a2b7ac3607b974e54d13e7162c1c72"
JSON_URL="https://github.com/nlohmann/json/releases/download/v3.11.3/json.hpp"
JSON_SHA="9bea4c8066ef4a1c206b2be5a36302f8926f7fdc6087af5d20b417d0cf103ea6"

if [ ! -d "$THIRD/eigen/Eigen" ]; then
  echo ">> fetching Eigen 3.4.0"
  curl -fsSL "$EIGEN_URL" -o "$DEPS/eigen-3.4.0.tar.gz"
  echo "$EIGEN_SHA  $DEPS/eigen-3.4.0.tar.gz" | sha256sum -c -
  tar xzf "$DEPS/eigen-3.4.0.tar.gz" -C "$THIRD"
  mv "$THIRD/eigen-3.4.0" "$THIRD/eigen"
fi

if [ ! -f "$THIRD/json/nlohmann/json.hpp" ]; then
  echo ">> fetching nlohmann/json v3.11.3"
  curl -fsSL "$JSON_URL" -o "$DEPS/json.hpp"
  echo "$JSON_SHA  $DEPS/json.hpp" | sha256sum -c -
  mkdir -p "$THIRD/json/nlohmann"
  cp "$DEPS/json.hpp" "$THIRD/json/nlohmann/json.hpp"
fi

echo ">> dependencies ready in $THIRD"
