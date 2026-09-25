#!/usr/bin/env bash
# 零依赖构建：仅需 JDK（javac），输出到 build/classes。
set -euo pipefail
cd "$(dirname "$0")"

OUT=build/classes
mkdir -p "$OUT"
find src -name '*.java' > build/sources.txt
echo ">> 编译 $(wc -l < build/sources.txt) 个 Java 源文件 -> $OUT"
javac -encoding UTF-8 -d "$OUT" @build/sources.txt
echo ">> 构建完成"
