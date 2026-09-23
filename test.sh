#!/usr/bin/env bash
# 编译并运行全部自动化测试（无 JUnit 依赖，自写断言 + 真实 HTTP 链路测试）。
set -euo pipefail
cd "$(dirname "$0")"
./build.sh
mkdir -p target/test-classes
find src/test/java -name '*.java' > target/test-sources.txt
javac -d target/test-classes -cp target/classes @target/test-sources.txt
java -cp target/classes:target/test-classes vecq.test.AllTests
