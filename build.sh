#!/usr/bin/env bash
# 编译全部源码与测试到 build/classes（零依赖，仅需 JDK 17+；开发环境为 JDK 21）。
set -euo pipefail
cd "$(dirname "$0")"

OUT=build/classes
rm -rf "$OUT"
mkdir -p "$OUT" build

find src -name '*.java' > build/sources.txt
javac -encoding UTF-8 -Xlint:all -d "$OUT" @build/sources.txt
echo "编译完成 -> $OUT"
