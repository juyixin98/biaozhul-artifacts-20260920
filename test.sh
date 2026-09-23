#!/usr/bin/env bash
# 运行全部自动化测试。退出码 0 表示全部通过。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d out ]; then
  ./build.sh
fi

java -cp out tvl.test.TestRunner
