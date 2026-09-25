#!/usr/bin/env bash
# 零依赖编译：仅需要 JDK 11+（开发与验证使用 JDK 21）。
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build/classes
find src -name '*.java' > build/sources.txt
javac -encoding UTF-8 -d build/classes @build/sources.txt
echo "compiled main sources -> build/classes"
