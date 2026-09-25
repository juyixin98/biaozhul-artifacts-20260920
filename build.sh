#!/usr/bin/env bash
# 编译主源码 + 测试源码到 build/（零外部依赖，仅需 JDK 17+）
set -euo pipefail
cd "$(dirname "$0")"

rm -rf build
mkdir -p build

echo "[1/2] 编译 src/ ..."
find src -name '*.java' > build/sources.txt
javac -d build @build/sources.txt

echo "[2/2] 编译 test/ ..."
find test -name '*.java' > build/test-sources.txt
javac -cp build -d build @build/test-sources.txt

echo "构建完成: build/"
