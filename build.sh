#!/usr/bin/env bash
# 编译主源码与测试源码（仅需 JDK，无外部依赖）
set -euo pipefail
cd "$(dirname "$0")"
rm -rf out
mkdir -p out/main out/test
find src/main/java -name '*.java' | sort > /tmp/boolsearch-main.sources
javac -encoding UTF-8 -d out/main @/tmp/boolsearch-main.sources
find src/test/java -name '*.java' | sort > /tmp/boolsearch-test.sources
javac -encoding UTF-8 -cp out/main -d out/test @/tmp/boolsearch-test.sources
echo "构建完成: out/main, out/test"
