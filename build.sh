#!/usr/bin/env bash
# 编译主代码（零运行时依赖，仅需 JDK 17+；开发使用 JDK 21）。
set -euo pipefail
cd "$(dirname "$0")"

OUT=target/classes
mkdir -p "$OUT"
find src/main/java -name '*.java' > target/main-sources.txt
javac -d "$OUT" --release 21 @target/main-sources.txt
echo "主代码编译完成 -> $OUT"
