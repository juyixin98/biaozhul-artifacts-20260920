#!/usr/bin/env bash
# 编译并运行全部自动化测试（零依赖自研测试框架）。
set -euo pipefail
cd "$(dirname "$0")/.."

bash scripts/build.sh

TEST_OUT=build/test-classes
mkdir -p "$TEST_OUT"
find src/test/java -name '*.java' > build/test-sources.txt
javac -encoding UTF-8 -cp build/classes -d "$TEST_OUT" @build/test-sources.txt

java -cp build/classes:build/test-classes phrase.test.AllTests
