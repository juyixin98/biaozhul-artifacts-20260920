#!/usr/bin/env bash
# 编译主源码与测试源码（仅依赖 JDK，无需 Maven/Gradle/网络）。
set -euo pipefail
cd "$(dirname "$0")"

JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}"
JAVAC="$JAVA_HOME/bin/javac"
[ -x "$JAVAC" ] || JAVAC="javac"

rm -rf out
mkdir -p out/classes out/test-classes
echo ">> 编译主源码..."
"$JAVAC" -d out/classes $(find src -name '*.java')
echo ">> 编译测试..."
"$JAVAC" -cp out/classes -d out/test-classes $(find test -name '*.java')
echo ">> 编译完成：out/classes, out/test-classes"
