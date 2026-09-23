#!/usr/bin/env bash
# 零依赖构建：仅需 JDK 17+（开发/验证使用 JDK 21）。无 Maven/Gradle。
set -euo pipefail
cd "$(dirname "$0")"

rm -rf build
mkdir -p build/classes build/test-classes

echo "[1/3] 编译主代码 -> build/classes"
find src/main/java -name '*.java' > build/main-sources.txt
javac -d build/classes @build/main-sources.txt

echo "[2/3] 编译测试 -> build/test-classes"
find src/test/java -name '*.java' > build/test-sources.txt
javac -d build/test-classes -cp build/classes @build/test-sources.txt

# 打包（方便分发运行）
echo "[3/3] 打包 build/dedup.jar"
jar --create --file build/dedup.jar --main-class dedup.server.DedupHttpServer -C build/classes .

echo "构建完成：build/classes, build/test-classes, build/dedup.jar"
