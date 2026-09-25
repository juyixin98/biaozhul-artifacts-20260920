#!/usr/bin/env bash
# 编译主源码与测试源码（仅依赖 JDK，无需 Maven/Gradle）。
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build
mkdir -p build/classes build/test-classes
echo "[build] compiling main sources..."
find src -name '*.java' > build/main-sources.txt
javac -d build/classes @build/main-sources.txt
echo "[build] compiling test sources..."
find test -name '*.java' > build/test-sources.txt
javac -d build/test-classes -cp build/classes @build/test-sources.txt
echo "[build] OK -> build/classes, build/test-classes"
