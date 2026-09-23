#!/usr/bin/env bash
# 编译主代码与测试代码到 out/ 目录（纯 javac，无外部依赖）
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf out
mkdir -p out

echo "== compiling main sources =="
javac -encoding UTF-8 -d out $(find src/main/java -name '*.java')

echo "== compiling test sources =="
javac -encoding UTF-8 -cp out -d out $(find src/test/java -name '*.java')

echo "build OK"
