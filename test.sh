#!/usr/bin/env bash
# 运行全部自动化测试（先编译，再执行 TestRunner）。
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"
if ! command -v "$JAVA_BIN" >/dev/null 2>&1; then
  echo "错误：找不到 java。请安装 JDK 17+ 或设置 JAVA_HOME。" >&2
  exit 1
fi

# 可选：-Dseed=<long> 固定属性测试的随机种子
"$JAVA_BIN" -cp build/classes:build/test-classes intervalindex.TestRunner "$@"
