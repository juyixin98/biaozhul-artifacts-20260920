#!/usr/bin/env bash
# 编译主代码与测试代码到 build/classes（零第三方依赖，仅需 JDK 11+）。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ]; then
  JAVAC="$JAVA_HOME/bin/javac"
else
  JAVAC="javac"
fi

rm -rf build/classes
mkdir -p build/classes

find src/main/java src/test/java -name '*.java' > build/sources.txt
$JAVAC -encoding UTF-8 -d build/classes @build/sources.txt
echo "Compiled -> build/classes"
