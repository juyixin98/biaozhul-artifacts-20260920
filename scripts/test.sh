#!/usr/bin/env bash
# 编译并运行全部自动化测试（零依赖测试运行器）。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="java"
fi

scripts/build.sh
$JAVA -cp build/classes intervaljoin.TestRunner
