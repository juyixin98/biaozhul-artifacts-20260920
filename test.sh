#!/usr/bin/env bash
# 运行全部自动化测试。用法: ./test.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build/classes ] || [ ! -d build/test-classes ]; then
  ./build.sh
fi

java -cp build/classes:build/test-classes dedup.tests.AllTests
