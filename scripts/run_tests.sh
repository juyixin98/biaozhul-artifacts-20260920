#!/usr/bin/env bash
# 编译并运行全部自动化测试（含真实子进程的端到端测试）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BUILD_DIR="$ROOT_DIR/build"
CLASSES_DIR="$BUILD_DIR/classes"
TEST_DIR="$BUILD_DIR/test-classes"

bash "$ROOT_DIR/scripts/build.sh"

rm -rf "$TEST_DIR"
mkdir -p "$TEST_DIR"

echo "[test] javac 测试源码 -> $TEST_DIR"
find "$ROOT_DIR/tests" -name '*.java' > "$BUILD_DIR/test-sources.txt"
javac -encoding UTF-8 -cp "$CLASSES_DIR" -d "$TEST_DIR" @"$BUILD_DIR/test-sources.txt"

echo "[test] 运行测试"
CP="$CLASSES_DIR:$TEST_DIR"
java -cp "$CP" windowengine.TestRunner \
  windowengine.WindowEngineTest \
  windowengine.RequestParserTest \
  windowengine.SerializationTest \
  windowengine.EndToEndTest
