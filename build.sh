#!/usr/bin/env bash
# 零依赖构建：仅使用 JDK 自带的 javac，不需要 Maven/Gradle/网络。
set -euo pipefail
cd "$(dirname "$0")"

OUT=build/classes
rm -rf "$OUT"
mkdir -p build "$OUT"

find src/main/java -name '*.java' > build/main-sources.txt
javac -encoding UTF-8 -d "$OUT" @build/main-sources.txt

echo "主源码编译完成 -> $OUT"
