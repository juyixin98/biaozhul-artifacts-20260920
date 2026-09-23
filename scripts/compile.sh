#!/usr/bin/env bash
# 编译主程序与测试到 out/ 目录。仅需 JDK 17+（javac），无第三方依赖。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/javac" ]; then
  JAVAC="$JAVA_HOME/bin/javac"
else
  JAVAC="$(command -v javac)"
fi

rm -rf out
mkdir -p out
find src test -name '*.java' > out/sources.txt
"$JAVAC" -encoding UTF-8 -d out @out/sources.txt
echo "编译完成 → out/"
