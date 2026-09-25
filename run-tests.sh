#!/usr/bin/env bash
# 运行全部自动化测试（退出码非 0 表示存在失败）。
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d build/classes ]; then
  ./build.sh
fi
javac -d build/test-classes -cp build/classes @build/test-sources.txt
java -cp build/classes:build/test-classes com.example.ac.test.AllTests
