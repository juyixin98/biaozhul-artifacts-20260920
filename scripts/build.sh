#!/usr/bin/env bash
# 编译主源码与测试源码（纯 JDK，无第三方依赖）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

rm -rf build
mkdir -p build/classes build/test-classes

echo "[build] compiling main sources..."
find src/main/java -name '*.java' > build/main-sources.txt
javac -encoding UTF-8 -d build/classes @build/main-sources.txt

echo "[build] compiling test sources..."
find src/test/java -name '*.java' > build/test-sources.txt
javac -encoding UTF-8 -cp build/classes -d build/test-classes @build/test-sources.txt

echo "[build] OK -> build/classes, build/test-classes"
