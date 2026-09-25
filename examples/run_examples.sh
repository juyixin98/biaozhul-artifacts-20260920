#!/usr/bin/env bash
# 端到端运行全部请求样例与合成验收；产物写入 out/。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p out

echo "### 1) 合成验收（阶跃/漂移/尖峰/干净噪声）"
python3 -m burst_detector acceptance --out out/acceptance

echo
echo "### 2) 生成示例 PCM 输入"
mkdir -p out/examples
python3 -m burst_detector generate-pcm out/examples/demo_input.pcm --kind spike

echo
echo "### 3) 逐个执行 examples/ 下的请求"
for f in examples/request_*.json; do
  echo "--- $f"
  python3 -m burst_detector detect "$f"
  echo
done
