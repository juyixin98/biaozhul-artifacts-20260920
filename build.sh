#!/usr/bin/env bash
# 仅依赖 JDK（javac/java），无 Maven/Gradle/外部库。
set -euo pipefail
cd "$(dirname "$0")"

rm -rf build
mkdir -p build
find src -name '*.java' > build/sources.txt
javac -d build @build/sources.txt
echo "main sources compiled -> build/"

find test -name '*.java' > build/test-sources.txt
javac -d build -cp build @build/test-sources.txt
echo "test sources compiled -> build/"
