#!/usr/bin/env bash
# 编译全部主源码到 out/。仅依赖 JDK 17+ 自带的 javac，无第三方依赖。
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA_HOME="${JAVA_HOME:-$HOME/jdk17/usr/lib/jvm/java-17-openjdk-amd64}"
JAVAC="$JAVA_HOME/bin/javac"

if [ ! -x "$JAVAC" ]; then
  JAVAC="$(command -v javac)"
fi

rm -rf out
mkdir -p out
"$JAVAC" -encoding UTF-8 -d out $(find src -name '*.java')
echo "编译完成 -> out/"
