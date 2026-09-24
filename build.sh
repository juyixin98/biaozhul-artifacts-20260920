#!/usr/bin/env bash
# 编译全部源码到 out/（仅需 JDK，无第三方依赖）。
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p out
find out -name '*.class' -delete
javac -encoding UTF-8 -d out $(find src -name '*.java')
echo "编译完成 -> out/"
