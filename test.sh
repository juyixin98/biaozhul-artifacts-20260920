#!/usr/bin/env bash
# 运行自动化测试：java joinorder.AllTests
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d build/classes ]; then
  ./build.sh >/dev/null
fi
java -cp build/classes joinorder.AllTests "$@"
