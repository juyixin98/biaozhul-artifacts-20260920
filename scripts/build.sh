#!/usr/bin/env bash
# 编译主代码与测试代码（纯 JDK，无第三方依赖）。
# 若 java 不在 PATH，可用 JAVA_HOME=/path/to/jdk ./scripts/build.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ]; then
  JAVAC="$JAVA_HOME/bin/javac"; JAVA="$JAVA_HOME/bin/java"
else
  JAVAC="${JAVAC:-javac}"; JAVA="${JAVA:-java}"
fi
$JAVAC -version

rm -rf build/classes build/test-classes
mkdir -p build/classes build/test-classes

find src/main/java -name '*.java' > build/main-sources.txt
$JAVAC -encoding UTF-8 -d build/classes @build/main-sources.txt

find src/test/java -name '*.java' > build/test-sources.txt
$JAVAC -encoding UTF-8 -cp build/classes -d build/test-classes @build/test-sources.txt

echo "BUILD OK"
