#!/usr/bin/env bash
# 纯 JDK 构建：无需 Maven/Gradle，无任何第三方依赖。
set -euo pipefail
cd "$(dirname "$0")"
SRC=build/classes
TST=build/test
rm -rf build
mkdir -p "$SRC" "$TST"
echo "[build] 编译主代码 -> $SRC"
find src/main/java -name '*.java' | sort > build/main-srcs.txt
javac -encoding UTF-8 -d "$SRC" @build/main-srcs.txt
echo "[build] 编译测试代码 -> $TST"
find src/test/java -name '*.java' | sort > build/test-srcs.txt
javac -encoding UTF-8 -cp "$SRC" -d "$TST" @build/test-srcs.txt
echo "[build] 完成"
