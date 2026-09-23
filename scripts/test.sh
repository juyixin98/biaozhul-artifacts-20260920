#!/usr/bin/env bash
# 编译并运行全部自动化测试。
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/build.sh
mkdir -p out/test
find test -name '*.java' > out/test-sources.txt
javac -d out/test -cp out/classes @out/test-sources.txt
exec java -Xmx256m -cp out/classes:out/test phj.AllTests
