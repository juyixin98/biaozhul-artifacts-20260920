#!/usr/bin/env bash
# 零外部依赖构建：只需要 JDK 17+（javac）。
set -euo pipefail
cd "$(dirname "$0")"

SRC_DIR="src/main/java"
OUT_DIR="build/classes"
SOURCES_FILE="build/sources.txt"

rm -rf build/classes
mkdir -p "$OUT_DIR"
find "$SRC_DIR" -name '*.java' > "$SOURCES_FILE"

echo "[build] 编译 $(wc -l < "$SOURCES_FILE") 个主源文件 -> $OUT_DIR"
javac -encoding UTF-8 -d "$OUT_DIR" @"$SOURCES_FILE"
echo "[build] 完成"
