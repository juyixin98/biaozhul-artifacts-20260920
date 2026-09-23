#!/usr/bin/env bash
# 编译主代码与测试代码到 build/classes（仅需 JDK 的 javac，零外部依赖）。
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf build/classes
mkdir -p build/classes
javac -d build/classes $(find src/main/java -name '*.java')
javac -cp build/classes -d build/classes $(find src/test/java -name '*.java')
echo "编译完成 -> build/classes"
