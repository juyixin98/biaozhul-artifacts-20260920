#!/usr/bin/env bash
# 编译主代码与测试到 build/classes（零第三方依赖，仅需 JDK 17+，开发使用 JDK 21）。
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build
mkdir -p build/classes
find src/main/java -name '*.java' > build/main.sources
javac -d build/classes @build/main.sources
find src/test/java -name '*.java' > build/test.sources
javac -d build/classes -cp build/classes @build/test.sources
echo "编译完成 -> build/classes"
