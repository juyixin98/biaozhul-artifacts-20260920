#!/usr/bin/env bash
# 编译主程序与测试，零第三方依赖，仅用 JDK 自带 javac。
set -euo pipefail
cd "$(dirname "$0")/.."

# 允许通过 JAVA_HOME 或环境中的 javac 指定 JDK；否则用 PATH 里的 javac。
if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/javac" ]; then
  JAVAC="$JAVA_HOME/bin/javac"
elif command -v javac >/dev/null 2>&1; then
  JAVAC="javac"
elif [ -x "$HOME/jdk21/usr/lib/jvm/java-21-openjdk-amd64/bin/javac" ]; then
  JAVAC="$HOME/jdk21/usr/lib/jvm/java-21-openjdk-amd64/bin/javac"
else
  echo "错误: 找不到 javac。请安装 JDK 11+ 或设置 JAVA_HOME。" >&2
  exit 1
fi

rm -rf build
mkdir -p build/classes build/test-classes

echo ">> 编译主程序: src/main/java -> build/classes"
find src/main/java -name '*.java' > build/main_sources.txt
"$JAVAC" -encoding UTF-8 --release 11 -d build/classes @build/main_sources.txt

echo ">> 编译测试: src/test/java -> build/test-classes"
find src/test/java -name '*.java' > build/test_sources.txt
"$JAVAC" -encoding UTF-8 --release 11 -cp build/classes -d build/test-classes @build/test_sources.txt

echo ">> 编译完成: build/classes, build/test-classes"
