#!/usr/bin/env bash
# 运行全部自动化测试（需要先执行 ./build.sh）。
set -euo pipefail
cd "$(dirname "$0")"
java -cp build/classes:build/test cdcrebuild.test.AllTests
