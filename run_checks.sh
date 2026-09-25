#!/usr/bin/env bash
# 一键运行：自动化测试 + 全部样例。无第三方依赖。
set -euo pipefail
cd "$(dirname "$0")"

echo "=== Python ==="
python3 --version

echo
echo "=== 自动化测试 ==="
python3 -m unittest discover -s tests

echo
echo "=== 样例分析 ==="
for f in examples/0*.tfl; do
  echo "--- $(basename "$f")"
  python3 -m taintflow.cli analyze "$f" | sed -n '2p'
done
