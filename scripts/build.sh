#!/usr/bin/env bash
# 零依赖构建：仅用 JDK 自带 javac，不需要 Maven/Gradle/网络。
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=build/classes
rm -rf build
mkdir -p "$OUT"

find src/main/java -name '*.java' > build/main-sources.txt
javac -encoding UTF-8 -Xlint:all -d "$OUT" @build/main-sources.txt

echo "Build OK -> $OUT"
