#!/usr/bin/env bash
# 编译主源码到 build/classes（零第三方依赖，仅需 JDK 17+）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BUILD_DIR="$ROOT_DIR/build"
CLASSES_DIR="$BUILD_DIR/classes"

rm -rf "$CLASSES_DIR"
mkdir -p "$CLASSES_DIR"

echo "[build] javac 主源码 -> $CLASSES_DIR"
find "$ROOT_DIR/src" -name '*.java' > "$BUILD_DIR/sources.txt"
javac -encoding UTF-8 -d "$CLASSES_DIR" @"$BUILD_DIR/sources.txt"

echo "[build] 完成"
