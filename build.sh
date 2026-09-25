#!/usr/bin/env bash
# 仅编译主源码到 build/classes（不运行测试）。
set -euo pipefail
cd "$(dirname "$0")"

OUT=build/classes
rm -rf "$OUT"
mkdir -p "$OUT"

find src/main/java -name '*.java' > build/sources-main.txt
javac -encoding UTF-8 -Xlint:all -d "$OUT" @build/sources-main.txt

echo "Main sources compiled into $OUT"
