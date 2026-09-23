#!/usr/bin/env bash
# 编译主代码与测试代码（仅需 JDK 17+ 的 javac，无第三方依赖）。
set -euo pipefail
cd "$(dirname "$0")"

SRC_DIR=src
OUT_DIR=build/classes
TEST_SRC=test
TEST_OUT=build/test-classes

rm -rf build
mkdir -p "$OUT_DIR" "$TEST_OUT"

# 定位 javac：使用 JAVA_HOME，否则回退到 PATH
JAVAC="${JAVA_HOME:+$JAVA_HOME/bin/}javac"
if ! command -v "$JAVAC" >/dev/null 2>&1; then
  echo "错误：找不到 javac。请安装 JDK 17+ 或设置 JAVA_HOME。" >&2
  exit 1
fi

echo ">> 使用 $($JAVAC -version 2>&1)"
find "$SRC_DIR" -name '*.java' > build/sources.txt
$JAVAC -encoding UTF-8 -Xlint:all -d "$OUT_DIR" @build/sources.txt
echo ">> 主代码编译完成 -> $OUT_DIR"

find "$TEST_SRC" -name '*.java' > build/test-sources.txt
$JAVAC -encoding UTF-8 -Xlint:all -cp "$OUT_DIR" -d "$TEST_OUT" @build/test-sources.txt
echo ">> 测试编译完成 -> $TEST_OUT"
