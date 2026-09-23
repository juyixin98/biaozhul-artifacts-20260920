#!/usr/bin/env bash
# 端到端冒烟脚本：启动服务 -> 提交/查询/取消 -> 观察老化与 FIFO 派发 -> 关停。
# 用法: ./examples/smoke.sh [监听地址，默认 127.0.0.1:18080]
#
# 分两个阶段，各自重新占槽，避免"占槽作业本身排在目标作业后面"的编排问题。
set -euo pipefail

ADDR="${1:-127.0.0.1:18080}"
BASE="http://$ADDR"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> 构建..."
( cd "$ROOT" && go build -o aq-server ./cmd/server )

echo ">> 启动服务（aging-step=1s, concurrency=2）..."
"$ROOT/aq-server" -addr "$ADDR" -aging-step 1s -max-concurrency 2 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 1

echo ">> healthz: $(curl -s $BASE/healthz)"

echo
echo "===== 阶段 A：排队取消 ====="
# 两个长 sleep 占满 2 个槽；victim 与 filler 排队。
curl -s -XPOST $BASE/jobs -d '{"id":"c1","type":"sleep","priority":9,"payload":{"ms":30000}}' >/dev/null
curl -s -XPOST $BASE/jobs -d '{"id":"c2","type":"sleep","priority":9,"payload":{"ms":30000}}' >/dev/null
curl -s -XPOST $BASE/jobs -d '{"id":"victim","type":"echo","priority":1}' >/dev/null
sleep 0.4
echo "   victim state = $(curl -s $BASE/jobs/victim | python3 -c 'import json,sys;print(json.load(sys.stdin)["job"]["state"])') （期望 queued）"
echo "   第一次取消   = $(curl -s -XPOST $BASE/jobs/victim/cancel | python3 -c 'import json,sys;print(json.load(sys.stdin)["outcome"])')"
code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST $BASE/jobs/victim/cancel || true)
echo "   重复取消 HTTP = $code （期望 409）"
code=$(curl -s -o /dev/null -w '%{http_code}' -XPOST $BASE/jobs/no-such/cancel || true)
echo "   取消不存在 HTTP = $code （期望 404）"

echo
echo "===== 阶段 B：老化 + 持续高优先级下低优先级先跑 ====="
# 关停阶段 A 的占槽作业（走运行中取消），让队列清空，再做老化演示。
curl -s -XPOST $BASE/jobs/c1/cancel >/dev/null
curl -s -XPOST $BASE/jobs/c2/cancel >/dev/null
sleep 0.5
# 两个 10s sleep 占满槽。
curl -s -XPOST $BASE/jobs -d '{"id":"hold1","type":"sleep","priority":9,"payload":{"ms":10000}}' >/dev/null
curl -s -XPOST $BASE/jobs -d '{"id":"hold2","type":"sleep","priority":9,"payload":{"ms":10000}}' >/dev/null
# 低优先级 + 持续注入高优先级。
curl -s -XPOST $BASE/jobs -d '{"id":"low","type":"echo","priority":0,"payload":"low-done"}' >/dev/null
curl -s -XPOST $BASE/jobs -d '{"id":"h1","type":"echo","priority":9}' >/dev/null
curl -s -XPOST $BASE/jobs -d '{"id":"h2","type":"echo","priority":9}' >/dev/null
curl -s -XPOST $BASE/jobs -d '{"id":"h3","type":"echo","priority":9}' >/dev/null
echo "   观察 low 老化（0 -> 9）..."
for i in 1 2 3; do
  sleep 3
  echo "     low effective_priority = $(curl -s $BASE/jobs/low | python3 -c 'import json,sys;print(json.load(sys.stdin)["job"]["effective_priority"])')"
done

echo ">> metrics（等待派发前）:"
curl -s $BASE/metrics; echo

echo ">> 等待 10s 占槽结束，查看 started 派发顺序..."
sleep 8
curl -s "$BASE/events?limit=300" | python3 -c "
import json,sys
ids=('hold1','hold2','low','h1','h2','h3')
for e in json.load(sys.stdin)['events']:
    if e['type']=='started' and e['job_id'] in ids:
        print('   ', e['at'][11:23], e['job_id'])
"
echo ">> 最终状态:"
for id in low h1 h2 h3; do
  curl -s $BASE/jobs/$id | python3 -c 'import json,sys;j=json.load(sys.stdin)["job"];print("   ",j["id"],j["state"],j.get("result",""))'
done
n=$(curl -s "$BASE/events?limit=300" | python3 -c 'import json,sys;print(sum(1 for e in json.load(sys.stdin)["events"] if e["job_id"]=="low" and e["type"]=="priority_boosted"))')
echo ">> low 的 priority_boosted 事件数 = $n （期望 9）"
echo ">> 完成。"
