#!/usr/bin/env bash
# 运行内置验收演练（三种场景 + 离线全量比对）。
# 用法: scripts/demo.sh [hotN]   hotN 默认 20000（热键每侧事件数）
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="java"
fi

scripts/build.sh
$JAVA -cp build/classes intervaljoin.Main demo "${1:-20000}"
