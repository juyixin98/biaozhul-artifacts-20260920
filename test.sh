#!/usr/bin/env bash
# 运行全部自动化测试（CoreTest / RecoveryTest / ServiceTest）。
# 退出码非 0 表示有断言失败。
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d build ] || [ -z "$(find build -name '*.class' -print -quit)" ]; then
  ./build.sh
fi
java -cp build com.example.cptx.tests.AllTests "$@"
