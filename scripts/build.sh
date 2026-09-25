#!/usr/bin/env bash
# 编译主代码与测试代码（零依赖，仅需 JDK）。
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p build/classes build/test-classes

echo "[build] compiling main sources..."
find src/main/java -name '*.java' > build/main-sources.txt
javac -encoding UTF-8 -d build/classes @build/main-sources.txt

echo "[build] compiling test sources..."
find src/test/java -name '*.java' > build/test-sources.txt
javac -encoding UTF-8 -cp build/classes -d build/test-classes @build/test-sources.txt

echo "[build] done -> build/classes, build/test-classes"
