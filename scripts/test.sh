#!/usr/bin/env bash
# 运行自动化测试（若未编译则先编译）。退出码非 0 表示有用例失败。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d build/classes/joinopt/AllTests.class ]; then
  ./scripts/build.sh
fi
java -cp build/classes joinopt.AllTests
