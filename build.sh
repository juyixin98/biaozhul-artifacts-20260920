#!/usr/bin/env bash
# 编译主源码与测试到 out/。仅依赖 JDK（在 Java 21 上验证），无需 Maven/Gradle 或联网。
set -euo pipefail
cd "$(dirname "$0")"

rm -rf out
mkdir -p out

echo "[build] compiling main sources + tests ..."
find src test -name '*.java' -print0 | xargs -0 javac -d out

echo "[build] done -> out/"
