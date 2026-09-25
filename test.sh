#!/usr/bin/env bash
# 编译并运行全部自动化测试。退出码非 0 表示存在失败。
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

TEST_OUT=build/test-classes
rm -rf "$TEST_OUT"
mkdir -p "$TEST_OUT"

find src/test/java -name '*.java' > build/test-sources.txt
javac -encoding UTF-8 -cp build/classes -d "$TEST_OUT" @build/test-sources.txt

java -Dfile.encoding=UTF-8 -cp build/classes:build/test-classes booleansearch.RunAllTests
