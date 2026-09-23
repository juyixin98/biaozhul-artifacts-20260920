#!/usr/bin/env bash
# 编译并运行全部自动化测试；成功退出 0，失败退出 1。
set -euo pipefail
cd "$(dirname "$0")"

echo "[test] 编译主代码 + 测试代码"
find src/main/java -name '*.java' > build/sources-main.txt
find src/test/java -name '*.java' > build/sources-test.txt
javac -encoding UTF-8 -d build/classes @build/sources-main.txt
javac -encoding UTF-8 -cp build/classes -d build/test-classes @build/sources-test.txt

echo "[test] 运行 dev.dedup.hll.AllTests"
java -cp "build/classes:build/test-classes" dev.dedup.hll.AllTests
