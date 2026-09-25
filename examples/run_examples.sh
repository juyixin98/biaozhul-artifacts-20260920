#!/usr/bin/env bash
# 运行 examples/ 下全部请求样例，响应逐行打到 stdout（JSON Lines）。
# 用法: bash examples/run_examples.sh
set -u
cd "$(dirname "$0")/.."
for f in examples/request_*.json; do
  echo "### $f"
  python3 -m adaptive_integration.cli "$f"
  echo
done
