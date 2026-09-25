#!/usr/bin/env bash
# 端到端请求样例: 启动服务后直接执行(需要 curl 与 jq)。
# 用法: bash examples/requests.sh [host:port]
set -euo pipefail

BASE="${1:-127.0.0.1:8000}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== 1. 健康检查 =="
curl -sS "http://${BASE}/health" | jq .

echo
echo "== 2. 索引统计 =="
curl -sS "http://${BASE}/stats" | jq .

echo
echo "== 3. 检索(entries 形态; 含重复维度 index=12 与负权重) =="
curl -sS "http://${BASE}/search" \
  -H 'Content-Type: application/json' \
  -d @"${HERE}/search_entries.json" | jq .

echo
echo "== 4. 检索(顶层 indices/values 形态, 省略 dim 回退到索引维度) =="
curl -sS "http://${BASE}/search" \
  -H 'Content-Type: application/json' \
  -d @"${HERE}/search_inline.json" | jq .

echo
echo "== 5. 零向量查询(余弦统一定义 0.0, 返回 doc_id 最小的 k 个) =="
curl -sS "http://${BASE}/search" \
  -H 'Content-Type: application/json' \
  -d @"${HERE}/search_zero_vector.json" | jq .

echo
echo "== 6. 用显式文档(含重复维度/负权重/零向量)重建索引 =="
curl -sS "http://${BASE}/index/rebuild" \
  -H 'Content-Type: application/json' \
  -d @"${HERE}/rebuild_docs.json" | jq .

echo
echo "== 7. 在重建后的小索引上检索 =="
curl -sS "http://${BASE}/search" \
  -H 'Content-Type: application/json' \
  -d '{"dim":6,"entries":[{"index":0,"value":1.0},{"index":1,"value":1.0}],"k":5}' | jq .

echo
echo "== 8. 用可复现合成数据重建 =="
curl -sS "http://${BASE}/index/rebuild" \
  -H 'Content-Type: application/json' \
  -d '{"synthetic":{"seed":7,"n_docs":500,"dim":200}}' | jq .

echo
echo "== 9. 错误样例(维度越界 -> HTTP 400) =="
curl -sS -w '\nHTTP %{http_code}\n' "http://${BASE}/search" \
  -H 'Content-Type: application/json' \
  -d '{"dim":200,"entries":[{"index":9999,"value":1.0}],"k":3}'
