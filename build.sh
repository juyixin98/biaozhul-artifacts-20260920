#!/usr/bin/env bash
# 编译主源码与测试源码。仅依赖 JDK 自带 javac，无第三方依赖。
set -euo pipefail
cd "$(dirname "$0")"

JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}javac"
JAVAC="${JAVAC:-$JAVA_BIN}"

mkdir -p out/classes out/test-classes
"$JAVAC" -d out/classes $(find src -name '*.java')
"$JAVAC" -cp out/classes -d out/test-classes $(find test -name '*.java')
echo "编译完成：out/classes、out/test-classes"
