#!/usr/bin/env bash
# 端到端演示：自动启动服务 -> 演练验收场景 -> 输出结果 -> 关闭服务。
# 用法: ./demo.sh [port]
set -uo pipefail
cd "$(dirname "$0")"
PORT="${1:-18080}"
BASE="http://localhost:$PORT"

if [ ! -d build ]; then
  ./build.sh
fi

java -cp build streamagg.service.Main "$PORT" > /tmp/streamagg-demo.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

echo "等待服务启动..."
for _ in $(seq 1 50); do
  if curl -s "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

say() { echo; echo "==================== $1 ===================="; }

say "健康检查"
curl -s "$BASE/health"

say "场景A：先撤销（v2，缓存）后新增（v1，级联撤销），最终聚合为 0"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"eA","op":"RETRACT","version":2,"opId":"A-retract"}'
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"eA","op":"ADD","key":"kA","value":100,"version":1,"opId":"A-add"}'
echo; echo "--- 重复提交撤销（opId 幂等）"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"eA","op":"RETRACT","version":2,"opId":"A-retract"}'

say "场景B：新增 + 多次更正（含乱序缓存级联）"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"eB","op":"ADD","key":"kB","value":10,"version":1}'
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"eB","op":"CORRECT","key":"kB","value":40,"version":4}'
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"eB","op":"CORRECT","key":"kB","value":30,"version":3}'
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"eB","op":"CORRECT","key":"kB","value":20,"version":2}'
echo; echo "--- v4 最终值 40，计数恒为 1"
curl -s "$BASE/stats?key=kB"

say "场景C：更正改键迁移（kB -> kC）"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"eB","op":"CORRECT","key":"kC","value":40,"version":5}'
curl -s "$BASE/stats"

say "场景D：防负漂移 —— 撤销从未新增的事件，计数不为负"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d '{"eventId":"ghost","op":"RETRACT","version":1}'
curl -s "$BASE/stats"

say "最终事件账本"
curl -s "$BASE/ledger"

say "账本重算对账（consistent 应为 true）"
curl -s -X POST "$BASE/reconcile" -d '{}'

say "日志重放（consistent 应为 true）"
curl -s -X POST "$BASE/replay" -d '{}' | head -3

echo; echo
echo "演示完成。"
