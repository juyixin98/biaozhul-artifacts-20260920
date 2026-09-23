#!/usr/bin/env bash
# 编译主源码（仅需 JDK，零第三方依赖）。
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT_DIR/src"
OUT_DIR="$ROOT_DIR/build/classes"

echo "[build] compiling sources from $SRC_DIR"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
find "$SRC_DIR" -name '*.java' > "$ROOT_DIR/build/sources.txt"
javac -encoding UTF-8 -d "$OUT_DIR" @"$ROOT_DIR/build/sources.txt"
echo "[build] OK -> $OUT_DIR"
