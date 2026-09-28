#!/usr/bin/env bash
# 需要先启动服务：
#   .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
set -euo pipefail
BASE=http://127.0.0.1:8000

echo "### health"
curl -s "$BASE/health"; echo

echo "### x^2 + 1（降序系数 [1,0,1]，根 ±i）"
curl -s -X POST "$BASE/api/v1/roots" \
  -H 'Content-Type: application/json' \
  -d '{"coefficients":[1,0,1],"run_id":"curl-x2p1"}' | python3 -m json.tool

echo "### 复系数 (1+i)x + 2，根 = -1+i"
curl -s -X POST "$BASE/api/v1/roots" \
  -H 'Content-Type: application/json' \
  -d '{"coefficients":[{"real":1,"imag":1},[2,0]]}' | python3 -m json.tool

echo "### 零多项式（必须被拒绝，400 input_error/zero_polynomial）"
curl -s -X POST "$BASE/api/v1/roots" \
  -H 'Content-Type: application/json' \
  -d '{"coefficients":[0,0,0]}'; echo

echo "### 读取历史运行"
curl -s "$BASE/api/v1/runs/curl-x2p1" | python3 -m json.tool
