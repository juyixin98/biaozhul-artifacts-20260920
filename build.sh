#!/usr/bin/env bash
# 零外部依赖构建：仅需 JDK（javac），不需要 Maven/Gradle。
set -euo pipefail
cd "$(dirname "$0")"

echo "[build] javac 编译 src/main/java -> build/classes"
find src/main/java -name '*.java' > build/sources-main.txt
javac -encoding UTF-8 -d build/classes @build/sources-main.txt
echo "[build] 完成。入口: dev.dedup.hll.Main / dev.dedup.hll.ErrorDistributionMain"
