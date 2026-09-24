#!/usr/bin/env bash
# 编译并运行全部自动化测试（零外部测试框架，JDK 内置断言运行器）。
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

TEST_SRC="src/test/java"
TEST_OUT="build/test-classes"
TEST_SOURCES="build/test-sources.txt"

rm -rf "$TEST_OUT"
mkdir -p "$TEST_OUT"
find "$TEST_SRC" -name '*.java' > "$TEST_SOURCES"

echo "[test] 编译 $(wc -l < "$TEST_SOURCES") 个测试源文件 -> $TEST_OUT"
javac -encoding UTF-8 -cp "build/classes" -d "$TEST_OUT" @"$TEST_SOURCES"

echo "[test] 运行"
java -cp "build/classes:$TEST_OUT" com.tvl.test.TestMain
