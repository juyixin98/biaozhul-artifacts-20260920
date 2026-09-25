#!/usr/bin/env bash
# 编译主源码与测试，并运行零依赖测试套件（退出码透传）。
set -euo pipefail
cd "$(dirname "$0")"

MAIN=build/classes
TEST=build/test-classes
mkdir -p build "$MAIN" "$TEST"

find src/main/java -name '*.java' > build/sources-main.txt
find src/test/java -name '*.java' > build/sources-test.txt

echo "== Compiling main =="
javac -encoding UTF-8 -Xlint:all -d "$MAIN" @build/sources-main.txt

echo "== Compiling tests =="
javac -encoding UTF-8 -Xlint:all -cp "$MAIN" -d "$TEST" @build/sources-test.txt

echo "== Running tests =="
java -cp "$MAIN:$TEST" com.bm25pager.Tests
