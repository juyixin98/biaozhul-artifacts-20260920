#!/usr/bin/env bash
# 编译并运行全部自动化测试（含验收对比）。退出码非零表示有用例失败。
set -euo pipefail
cd "$(dirname "$0")/.."
JAVA_BIN="$(bash scripts/find-java.sh)"
mkdir -p target/classes target/test-classes
echo ">> compiling main..."
"$JAVA_BIN/javac" --release 11 -encoding UTF-8 -d target/classes $(find src -name '*.java')
echo ">> compiling tests..."
"$JAVA_BIN/javac" --release 11 -encoding UTF-8 -cp target/classes -d target/test-classes $(find test -name '*.java')
echo ">> running tests..."
"$JAVA_BIN/java" -cp target/classes:target/test-classes ij.TestRunner
