#!/usr/bin/env bash
# 编译全部源码到 build/classes。仅需 JDK 17+，不下载任何依赖。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build

find_javac() {
  if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/javac" ]; then
    echo "$JAVA_HOME/bin/javac"; return
  fi
  if command -v javac >/dev/null 2>&1; then
    command -v javac; return
  fi
  # 仓库内置的免安装 JDK（.tools/jdk-*）
  local cand
  cand=$(ls -d .tools/jdk-*/bin/javac 2>/dev/null | sort -V | tail -1 || true)
  if [ -n "$cand" ]; then echo "$cand"; return; fi
  echo ""
}

JAVAC="$(find_javac)"
if [ -z "$JAVAC" ]; then
  echo "ERROR: JDK 17+ with javac not found. Install a JDK or set JAVA_HOME." >&2
  exit 1
fi

echo "Using $($JAVAC -version 2>&1)"
rm -rf build/classes build/classes-test
mkdir -p build/classes build/classes-test
find src -name '*.java' > build/sources.txt
$JAVAC -encoding UTF-8 -d build/classes @build/sources.txt
echo "compiled main -> build/classes"

if [ "${1:-}" = "--with-tests" ]; then
  find test -name '*.java' > build/test-sources.txt
  $JAVAC -encoding UTF-8 -cp build/classes -d build/classes-test @build/test-sources.txt
  echo "compiled tests -> build/classes-test"
fi
