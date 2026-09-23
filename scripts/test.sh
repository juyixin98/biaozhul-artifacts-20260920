#!/usr/bin/env bash
# 编译并运行全部自动化测试（零依赖迷你测试框架）。
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT_DIR/src"
TEST_DIR="$ROOT_DIR/test"
CLASSES_DIR="$ROOT_DIR/build/classes"
TEST_CLASSES_DIR="$ROOT_DIR/build/test-classes"

echo "[test] compiling main + test sources"
rm -rf "$CLASSES_DIR" "$TEST_CLASSES_DIR"
mkdir -p "$CLASSES_DIR" "$TEST_CLASSES_DIR"
find "$SRC_DIR" "$TEST_DIR" -name '*.java' > "$ROOT_DIR/build/all-sources.txt"
javac -encoding UTF-8 -d "$TEST_CLASSES_DIR" @"$ROOT_DIR/build/all-sources.txt"

echo "[test] running TestRunner"
java -cp "$TEST_CLASSES_DIR" com.tjoin.TestRunner
