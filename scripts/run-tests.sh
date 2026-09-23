#!/usr/bin/env bash
# 编译并运行全部单元/集成测试。
set -euo pipefail
cd "$(dirname "$0")/.."

"$(dirname "$0")/build.sh" >/dev/null

if [ -n "${JAVA_HOME:-}" ]; then JAVA="$JAVA_HOME/bin/java"; else JAVA="${JAVA:-java}"; fi

$JAVA -cp build/classes:build/test-classes vecsearch.TestAll
