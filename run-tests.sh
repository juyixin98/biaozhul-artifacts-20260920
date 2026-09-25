#!/usr/bin/env bash
# 运行全部自动化测试（需先执行 ./build.sh）。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build/classes ]; then
  ./build.sh
fi
java -cp build/classes tests.RunTests
