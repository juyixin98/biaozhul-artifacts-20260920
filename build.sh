#!/usr/bin/env bash
# 编译主程序到 out/（仅需 JDK，无第三方依赖）
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p out
find src -name '*.java' > /tmp/cdc-sources.txt
javac -d out @/tmp/cdc-sources.txt
echo "编译完成 -> out/"
