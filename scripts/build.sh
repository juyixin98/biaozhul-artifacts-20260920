#!/usr/bin/env bash
# 编译主代码与测试代码（零第三方依赖，仅用 JDK 自带类）
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ]; then
  JAVAC="$JAVA_HOME/bin/javac"
else
  JAVAC="$(command -v javac)"
fi
"$JAVAC" -version

rm -rf build
mkdir -p build/classes build/test-classes

find src -name '*.java' > build/sources.txt
find test -name '*.java' > build/test-sources.txt

"$JAVAC" -encoding UTF-8 -d build/classes @build/sources.txt
"$JAVAC" -encoding UTF-8 -cp build/classes -d build/test-classes @build/test-sources.txt

echo "编译完成：build/classes, build/test-classes"
