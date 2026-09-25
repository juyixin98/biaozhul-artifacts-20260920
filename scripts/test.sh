#!/usr/bin/env bash
# 运行全部自动化测试。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d build/classes ] || [ ! -d build/test-classes ]; then
  ./scripts/build.sh
fi

java -cp build/classes:build/test-classes com.example.segmenter.tests.AllTests
