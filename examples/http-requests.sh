#!/usr/bin/env bash
# ws-server HTTP 接口请求样例。
# 用法：
#   go run ./cmd/ws-server -addr 127.0.0.1:8080 -workers 4
#   bash examples/http-requests.sh
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8080}
j() { python3 -m json.tool; }   # 没有 python3 时可改成 cat

echo "== 健康检查 =="
curl -s "$BASE/healthz" | j; echo

echo "== 创建一个 4 worker 的执行器（Chase-Lev 队列）=="
curl -s -X POST "$BASE/api/executors" \
  -H 'Content-Type: application/json' \
  -d '{"name":"demo","workers":4,"deque":"chaselev"}' | j; echo

echo "== 再创建一个单 worker 执行器（用于观察 help-the-child）=="
curl -s -X POST "$BASE/api/executors" \
  -H 'Content-Type: application/json' \
  -d '{"name":"solo","workers":1}' | j; echo

echo "== 提交一个 noop =="
curl -s -X POST "$BASE/api/executors/demo/tasks" \
  -H 'Content-Type: application/json' \
  -d '{"type":"noop","name":"hello"}' | j; echo

echo "== 提交一个可取消的 sleep（5 秒）=="
SLEEP=$(curl -s -X POST "$BASE/api/executors/demo/tasks" \
  -H 'Content-Type: application/json' \
  -d '{"type":"sleep","name":"sleeper","params":{"ms":5000}}')
echo "$SLEEP" | j
SID=$(echo "$SLEEP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

sleep 0.2
echo "== 查询该任务（应为 running）=="
curl -s "$BASE/api/executors/demo/tasks/$SID" | j; echo

echo "== 取消该任务 =="
curl -s -X POST "$BASE/api/executors/demo/tasks/$SID/cancel" \
  -H 'Content-Type: application/json' -d '{}' | j; echo

echo "== 单 worker 下提交一棵深递归任务树（depth=8, fanout=2 => 511 节点）=="
TREE=$(curl -s -X POST "$BASE/api/executors/solo/tasks" \
  -H 'Content-Type: application/json' \
  -d '{"type":"tree","name":"deep","params":{"depth":8,"fanout":2,"work_ms":0}}')
echo "$TREE" | j
TID=$(echo "$TREE" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

for i in 1 2 3 4 5 6 7 8 9 10; do
  ST=$(curl -s "$BASE/api/executors/solo/tasks/$TID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  echo "  poll $i: $ST"
  if [ "$ST" = "completed" ] || [ "$ST" = "failed" ] || [ "$ST" = "canceled" ]; then break; fi
  sleep 0.2
done

echo "== 查看执行器统计（steals/completed/canceled 等）=="
curl -s "$BASE/api/executors/demo" | j; echo
curl -s "$BASE/api/executors/solo" | j; echo

echo "== 创建周期调度（每 200ms 一个 noop）=="
SCH=$(curl -s -X POST "$BASE/api/executors/demo/schedules" \
  -H 'Content-Type: application/json' \
  -d @examples/schedule.json)
echo "$SCH" | j
HID=$(echo "$SCH" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

sleep 0.7
echo "== 撤销周期调度 =="
curl -s -X DELETE "$BASE/api/executors/demo/schedules/$HID" | j; echo

echo "== 订阅结构化事件流（实时 3 秒；新终端用: curl -N .../events）=="
timeout 3 curl -sN "$BASE/api/executors/demo/events" | head -n 20 || true

echo
echo "== 优雅关闭并删除执行器 =="
curl -s -X DELETE "$BASE/api/executors/solo" | j; echo
curl -s -X DELETE "$BASE/api/executors/demo" | j; echo
