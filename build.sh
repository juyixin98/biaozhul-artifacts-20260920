#!/usr/bin/env bash
# 三值逻辑查询引擎 —— 零依赖构建与测试脚本。
# 仅需 JDK 17+（javac/java），不需要 Maven/Gradle 或联网。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MAIN_OUT=build/classes
TEST_OUT=build/test-classes

echo "==> 清理 build/"
rm -rf build
mkdir -p "$MAIN_OUT" "$TEST_OUT"

echo "==> 编译主代码 -> $MAIN_OUT"
find src/main/java -name '*.java' > build/main-sources.txt
javac -encoding UTF-8 -d "$MAIN_OUT" @build/main-sources.txt

echo "==> 编译测试代码 -> $TEST_OUT"
find src/test/java -name '*.java' > build/test-sources.txt
javac -encoding UTF-8 -cp "$MAIN_OUT" -d "$TEST_OUT" @build/test-sources.txt

echo "==> 编译完成"
echo
echo "运行测试：    ./run-tests.sh   或  bash run-tests.sh"
echo "执行查询：    java -cp $MAIN_OUT tvl.cli.Main query samples/query-basic.json"
echo "查看真值表：  java -cp $MAIN_OUT tvl.cli.Main truth-table"
