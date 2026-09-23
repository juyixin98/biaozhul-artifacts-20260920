#!/usr/bin/env bash
# 编译并运行全部自动化测试
set -euo pipefail
cd "$(dirname "$0")/.."

./scripts/build.sh
echo
echo "== running tests =="
java -cp out com.bm25stable.TestMain
