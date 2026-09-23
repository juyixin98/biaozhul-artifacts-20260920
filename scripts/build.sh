#!/usr/bin/env bash
# 编译主源码与零依赖测试到 build/。
# 可用环境变量 JAVA_HOME 指定 JDK；否则使用 PATH 中的 javac。
set -euo pipefail
cd "$(dirname "$0")/.."

JAVAC="${JAVA_HOME:+$JAVA_HOME/bin/}javac"
if ! command -v "$JAVAC" >/dev/null 2>&1; then
  echo "错误：找不到 javac。请安装 JDK 17+，或设置 JAVA_HOME。" >&2
  exit 1
fi

echo "使用: $("$JAVAC" -version 2>&1)"
rm -rf build
mkdir -p build/classes build/test-classes
$JAVAC -d build/classes $(find src/main -name '*.java')
$JAVAC -cp build/classes -d build/test-classes $(find src/test -name '*.java')
echo "编译完成 -> build/classes, build/test-classes"
