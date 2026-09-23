#!/usr/bin/env bash
# 运行固定种子误差分布实验，输出 results/error-distribution/report.{json,md}
set -euo pipefail
cd "$(dirname "$0")"
java -cp build/classes dev.dedup.hll.ErrorDistributionMain results/error-distribution
