#!/usr/bin/env bash
# 编译并运行全部自动化测试；有失败时以退出码 1 结束。
set -euo pipefail
cd "$(dirname "$0")"
./build.sh
mkdir -p build/test-classes
find test -name '*.java' > build/test-sources.txt
javac -encoding UTF-8 -cp build/classes -d build/test-classes @build/test-sources.txt
echo "compiled tests -> build/test-classes"
echo "----------------------------------------"
java -cp build/classes:build/test-classes com.example.edcand.AllTests
