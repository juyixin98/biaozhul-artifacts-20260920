#!/usr/bin/env bash
# 一键构建 + 校验依赖 + 跑单元测试。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/3 校验锁定依赖 =="
./third_party/verify_deps.sh

echo "== 2/3 构建 =="
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j

echo "== 3/3 单元测试 =="
./build/tf_tests

echo
echo "完成。启动服务: ./build/tf_server --host 127.0.0.1 --port 18080"
