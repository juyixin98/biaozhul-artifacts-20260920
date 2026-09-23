#!/usr/bin/env bash
# 编译并运行全部自动化测试（含真实子进程的崩溃恢复测试）。
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

if [[ -n "${JAVA_HOME:-}" ]]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="$(command -v java)"
fi

"$JAVA" -cp build/classes:build/test-classes cep.AllTests
