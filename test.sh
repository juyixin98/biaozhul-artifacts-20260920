#!/usr/bin/env bash
# 编译并运行全部自动化测试（零依赖迷你测试框架）
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p out out-test
find src test -name '*.java' > /tmp/cdc-all-sources.txt
javac -d out-test @/tmp/cdc-all-sources.txt
java -cp out-test com.example.cdc.RunTests
