#!/usr/bin/env bash
# 编译主源码与测试源码到 build/。纯 JDK，不下载任何依赖。
set -euo pipefail
cd "$(dirname "$0")"

JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}javac"
if ! command -v "$JAVA_BIN" >/dev/null 2>&1; then
  echo "error: javac not found. Set JAVA_HOME to a JDK 17+ installation." >&2
  exit 1
fi

rm -rf build
mkdir -p build/classes build/test
$JAVA_BIN -encoding UTF-8 -Xlint:all -d build/classes $(find src/main/java -name '*.java')
$JAVA_BIN -encoding UTF-8 -Xlint:all -cp build/classes -d build/test $(find src/test/java -name '*.java')
echo "build OK -> build/classes, build/test"
