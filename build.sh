#!/usr/bin/env bash
# 编译全部源码与测试，零第三方依赖，仅需 JDK 17+。
set -euo pipefail
cd "$(dirname "$0")"

if [[ -n "${JAVA_HOME:-}" ]]; then
  JAVAC="$JAVA_HOME/bin/javac"
  JAVA="$JAVA_HOME/bin/java"
else
  JAVAC="$(command -v javac)"
  JAVA="$(command -v java)"
fi

if [[ -z "${JAVAC:-}" ]]; then
  echo "错误: 未找到 javac。请安装 JDK 17+，或设置 JAVA_HOME。" >&2
  exit 1
fi

echo "使用编译器: $("$JAVAC" -version 2>&1)"

SRC_OUT=build/classes
TEST_OUT=build/test-classes
rm -rf "$SRC_OUT" "$TEST_OUT"
mkdir -p "$SRC_OUT" "$TEST_OUT"

echo "== 编译主源码 =="
find src -name '*.java' > build/sources.txt
"$JAVAC" -encoding UTF-8 -Xlint:all,-serial -Werror -d "$SRC_OUT" @build/sources.txt

echo "== 编译测试 =="
find test -name '*.java' > build/test-sources.txt
"$JAVAC" -encoding UTF-8 -Xlint:all,-serial -Werror -cp "$SRC_OUT" -d "$TEST_OUT" @build/test-sources.txt

echo "编译完成: $SRC_OUT, $TEST_OUT"
