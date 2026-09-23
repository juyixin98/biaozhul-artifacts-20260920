#!/usr/bin/env bash
# 编译主程序到 build/classes。仅需 JDK（javac），无第三方依赖。
set -euo pipefail
cd "$(dirname "$0")"
JAVAC="${JAVAC:-javac}"
rm -rf build/classes
mkdir -p build/classes
"$JAVAC" --release 17 -d build/classes $(find src/main -name '*.java')
echo "编译完成 -> build/classes"
