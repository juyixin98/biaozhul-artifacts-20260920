#!/usr/bin/env bash
# 零依赖构建：仅使用 JDK 自带 javac，不需要 Maven/Gradle。
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf out
mkdir -p out
find src -name '*.java' > /tmp/ppd_sources.txt
javac -d out @/tmp/ppd_sources.txt
echo "构建完成 -> out/"
