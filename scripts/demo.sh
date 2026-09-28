#!/usr/bin/env bash
# 本地演示：1) 重放全部规范场景（含逐字段成本树） 2) 启动 HTTP 服务打真实请求
# 用法：bash scripts/demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-3100}"
LOG="${RUN_LOG:-logs/demo-runs.jsonl}"
mkdir -p logs
: > "$LOG"

echo "############ 1) 规范场景重放 ############"
RUN_LOG="$LOG" npx tsx src/diag/replay.ts | tee logs/demo-replay.txt

echo
echo "############ 2) HTTP 服务（端口 $PORT）############"
PORT="$PORT" RUN_LOG="$LOG" npx tsx src/server.ts &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

# 等待端口就绪
for _ in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

echo "--- GET /health"
curl -s "http://127.0.0.1:$PORT/health"; echo

echo "--- POST /query（S4：估计 921 < 实际 1609，预算 1200，预期 PARTIAL）"
curl -s -X POST "http://127.0.0.1:$PORT/query" -H 'content-type: application/json' -d '{
  "runId": "http-S4",
  "budget": 1200,
  "query": "{ users { id posts { id tags { name weight } } } }"
}'; echo

echo "--- POST /query（静态预算门拒绝，预期 507）"
curl -s -o /tmp/budget-gateway-507.json -w "http_status=%{http_code}\n" -X POST "http://127.0.0.1:$PORT/query" \
  -H 'content-type: application/json' \
  -d '{"budget": 100, "query": "{ users { id name posts { id title } } }"}'
cat /tmp/budget-gateway-507.json; echo

echo "--- GET /runs（运行编号与判定汇总）"
curl -s "http://127.0.0.1:$PORT/runs"; echo

echo
echo "演示完成，日志见 $LOG 与 logs/demo-replay.txt"
