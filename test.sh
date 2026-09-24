#!/usr/bin/env bash
# 运行自动化测试（失败时退出码非 0）。
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p out
javac -encoding UTF-8 -d out $(find src -name '*.java')
java -cp out bitserver.TestRunner
