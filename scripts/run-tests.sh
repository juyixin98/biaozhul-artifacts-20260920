#!/usr/bin/env bash
# 运行全部自动化测试（失败退出码非 0）。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="$(command -v java)"
fi

# 每次都重新编译，避免陈旧 class 文件造成误判。
./scripts/compile.sh

"$JAVA" -cp out tumbling.TestRunner
