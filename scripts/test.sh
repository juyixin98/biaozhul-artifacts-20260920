#!/usr/bin/env bash
# 运行零依赖自动化测试（先编译）。退出码非 0 即有测试失败。
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA="${JAVA_HOME:+$JAVA_HOME/bin/}java"
JAVAC="${JAVA_HOME:+$JAVA_HOME/bin/}javac"

if [ ! -d build/classes ]; then
  scripts/build.sh
fi
$JAVAC -cp build/classes -d build/test-classes $(find src/test -name '*.java')
$JAVA -cp build/classes:build/test-classes colscan.Tests
