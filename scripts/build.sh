#!/usr/bin/env bash
# 编译主程序到 out/classes（零外部依赖，仅需 JDK 17+）。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p out/classes
find src/main/java -name '*.java' > out/sources.txt
javac -d out/classes @out/sources.txt
echo "编译完成：out/classes"
