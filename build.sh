#!/usr/bin/env bash
# 零依赖构建：仅用 JDK 自带 javac，无 Maven/Gradle/外部库。
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build
mkdir -p build
find src -name '*.java' > build/sources.txt
javac -d build @build/sources.txt
echo "构建完成 -> build/"
