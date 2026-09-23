#!/usr/bin/env bash
# 编译主代码 + 测试代码并运行自动化测试（自建断言框架，无第三方依赖）。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p out
find src test -name '*.java' > /tmp/ppd_all_sources.txt
javac -d out @/tmp/ppd_all_sources.txt
java -cp out ppd.tests.TestRunner
