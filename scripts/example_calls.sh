#!/usr/bin/env bash
# 示例调用：先启动服务（.venv/bin/python -m app.main），再执行本脚本。
set -euo pipefail
BASE="${SUMCMP_BASE:-http://127.0.0.1:8429}"

echo '== healthz =='
curl -s "$BASE/healthz"; echo

echo '== 固定规则 =='
curl -s "$BASE/v1/policies"; echo

echo '== 经典相消 [1e16, 1, -1e16] 三方法对比 =='
curl -s -X POST "$BASE/v1/compare" -H 'content-type: application/json' \
  -d '{"values": [1e16, 1.0, -1e16], "chunk_size": 2}'; echo

echo '== 大流式小数累积 0.1 x 1,000,000 =='
curl -s -X POST "$BASE/v1/compare" -H 'content-type: application/json' \
  -d '{"generator": {"kind": "small_accumulation", "count": 1000000, "value": 0.1}, "chunk_size": 8192}'; echo

echo '== 重排探针（12 次块序重排） =='
curl -s -X POST "$BASE/v1/compare" -H 'content-type: application/json' \
  -d '{"generator": {"kind": "random_spread", "n": 4096, "seed": 11}, "chunk_size": 64, "reorder_trials": 12}'; echo

echo '== NaN 拒绝（自带请求标识） =='
curl -s -X POST "$BASE/v1/sum" -H 'content-type: application/json' -H 'X-Request-ID: demo-nan-001' \
  -d '{"values": [1.0, NaN], "method": "naive"}'; echo
