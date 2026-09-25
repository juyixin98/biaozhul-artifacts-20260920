#!/usr/bin/env bash
# 分析一个 .rf 源码文件并打印 JSON 报告。
# 用法: bash analyze.sh examples/exceptional_exit.rf [loop_bound]
set -euo pipefail
cd "$(dirname "$0")"
FILE="${1:?usage: bash analyze.sh <file.rf> [loop_bound]}"
BOUND="${2:-2}"
python3 -m resflow.cli analyze "$FILE" --loop-bound "$BOUND"
