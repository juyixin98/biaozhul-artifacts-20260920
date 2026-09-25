#!/usr/bin/env bash
# 请求样例：先启动服务
#   go run ./cmd/logcluster -addr :8080 -data /tmp/lc-state.json
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"

echo "== health =="
curl -s "$BASE/healthz"

echo; echo "== ingest: 数字/UUID 变量被掩码，关键字差异保留 =="
curl -s -X POST "$BASE/v1/logs" -d '{
  "lines": [
    "GET /api/v1/orders/1001 completed in 12ms status=200",
    "GET /api/v1/orders/2048 completed in 87ms status=200",
    "connection error: timed out after 3000ms",
    "connection error: refused by remote host",
    "event 550e8400-e29b-41d4-a716-446655440000 processed by worker-3",
    "authentication failed for user=alice from 10.1.2.3"
  ]
}'

echo; echo "== ingest: 触发版本化泛化（NUM 位置出现 UUID -> v2） =="
curl -s -X POST "$BASE/v1/logs" -d '{"lines": [
  "lookup key 12345 done",
  "lookup key 550e8400-e29b-41d4-a716-446655440000 done"
]}'

echo; echo "== templates =="
curl -s "$BASE/v1/templates"

echo; echo "== template #1 详情（含版本历史） =="
curl -s "$BASE/v1/templates/1"

echo; echo "== stats（含淘汰日志） =="
curl -s "$BASE/v1/stats"

echo; echo "== 立即快照（需 -data 启动） =="
curl -s -X POST "$BASE/v1/snapshot"
