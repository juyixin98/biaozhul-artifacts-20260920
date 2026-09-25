#!/usr/bin/env bash
# 运行全部自动化测试（先构建）。
set -euo pipefail
cd "$(dirname "$0")"
./build.sh
java -cp build/classes drvb.tests.TestRunner build/classes
