#!/usr/bin/env bash
# 编译主程序源码到 target/classes（零第三方依赖，仅 JDK 内置库）。
set -euo pipefail
cd "$(dirname "$0")/.."
JAVA_BIN="$(bash scripts/find-java.sh)"
mkdir -p target/classes
"$JAVA_BIN/javac" --release 11 -encoding UTF-8 -d target/classes $(find src -name '*.java')
echo "build ok -> target/classes ($("$JAVA_BIN/javac" -version 2>&1))"
