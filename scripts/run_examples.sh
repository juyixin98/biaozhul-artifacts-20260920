#!/usr/bin/env bash
# 一键复现全部命令行样例（仓库根目录执行）：bash scripts/run_examples.sh
# 每次运行使用带时间戳的输出目录，避免触发"拒绝覆盖"保护。
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="example_runs/$STAMP"
mkdir -p "$OUT"

python3 examples/generate_example_data.py

for name in synthetic negative_delay pcm wav_windowed periodic_sine silence; do
  echo
  echo "########## $name ##########"
  python3 -m delay_correlator run "examples/request_${name}.json" -o "$OUT/$name"
done

echo
echo "全部结果位于: $OUT"
