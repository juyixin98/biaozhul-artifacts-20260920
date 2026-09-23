#!/usr/bin/env bash
# 编译主源码到 target/classes（无外部依赖，仅需 JDK 17+，在 JDK 21 上验证）。
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p target/classes
find src/main/java -name '*.java' > target/main-sources.txt
javac -d target/classes @target/main-sources.txt
echo "编译完成: target/classes"
