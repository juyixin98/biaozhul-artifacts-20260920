#!/usr/bin/env bash
# 零依赖构建：只用 javac，不需要 Maven/Gradle。
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=build/classes
rm -rf build
mkdir -p build "$OUT"

echo "[build] 编译 src -> $OUT"
find src -name '*.java' > build/sources.txt
javac -encoding UTF-8 -d "$OUT" @build/sources.txt

echo "[build] 编译 tests -> $OUT"
find tests -name '*.java' > build/test-sources.txt
javac -encoding UTF-8 -cp "$OUT" -d "$OUT" @build/test-sources.txt

echo "[build] 完成"
