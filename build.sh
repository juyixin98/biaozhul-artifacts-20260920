#!/usr/bin/env bash
# 裸 JDK 构建：编译主源码与测试到 build/classes，无需 Maven/Gradle/外部依赖。
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p build/classes
find src tests -name '*.java' > build/sources.txt
echo "编译 $(wc -l < build/sources.txt) 个 Java 源文件..."
javac -d build/classes @build/sources.txt
echo "构建完成：build/classes"
