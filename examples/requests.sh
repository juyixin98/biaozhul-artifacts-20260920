#!/usr/bin/env bash
# 请求样例：先启动服务（见 README），再执行本脚本。
# 用法: ./requests.sh [port]
set -euo pipefail
PORT="${1:-18080}"
BASE="http://127.0.0.1:${PORT}"
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="${DIR}/out.jsonl"

echo "== 1. 健康检查 =="
curl -s "${BASE}/health"; echo

echo "== 2. 同步等值连接（小内存预算 4096，强制溢写） =="
curl -s -X POST "${BASE}/join/sync" -H 'Content-Type: application/json' -d "{
  \"leftPath\": \"${DIR}/left.jsonl\",
  \"rightPath\": \"${DIR}/right.jsonl\",
  \"keyField\": \"id\",
  \"outputPath\": \"${OUT}\",
  \"memoryBudgetBytes\": 4096
}"; echo

echo "== 3. 输出文件行数与样例行 =="
wc -l "${OUT}"
head -n 2 "${OUT}"

echo "== 4. 异步提交 + 轮询状态 =="
TASK=$(curl -s -X POST "${BASE}/join" -H 'Content-Type: application/json' -d "{
  \"leftPath\": \"${DIR}/left.jsonl\",
  \"rightPath\": \"${DIR}/right.jsonl\",
  \"keyField\": \"id\",
  \"outputPath\": \"${DIR}/out-async.jsonl\",
  \"memoryBudgetBytes\": 4096
}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["taskId"])')
echo "taskId=${TASK}"
curl -s "${BASE}/tasks/${TASK}"; echo

echo "== 5. 取消一个任务（先提交一个大任务再取消） =="
BIG=$(curl -s -X POST "${BASE}/join" -H 'Content-Type: application/json' -d "{
  \"leftPath\": \"${DIR}/left.jsonl\",
  \"rightPath\": \"${DIR}/right.jsonl\",
  \"keyField\": \"id\",
  \"outputPath\": \"${DIR}/out-cancel.jsonl\",
  \"memoryBudgetBytes\": 4096
}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["taskId"])')
curl -s -X POST "${BASE}/tasks/${BIG}/cancel"; echo
sleep 0.3
curl -s "${BASE}/tasks/${BIG}"; echo
