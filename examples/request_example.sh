#!/usr/bin/env bash
# 请求样例:先启动服务(python3 -m calibration_eval.service),再执行本脚本。
set -euo pipefail
cd "$(dirname "$0")"

echo "== 健康检查 =="
curl -s http://127.0.0.1:8000/health
echo

echo "== 正常评估请求 =="
curl -s -X POST http://127.0.0.1:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d @request_example.json
echo

echo "== 非法概率(应返回 400) =="
curl -s -X POST http://127.0.0.1:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"y_true": [0, 1], "y_prob": [-0.1, 0.5]}'
echo
