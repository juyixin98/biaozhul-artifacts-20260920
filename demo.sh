#!/usr/bin/env bash
# 一键端到端演示（真实启动 HTTP 服务 + curl）。
# 使用 manual 时钟，确定性演示：交错规则更新、乱序/晚到事件、边界时刻、
# 回滚、历史版本回收前提与回收后拒绝。输出同时写入 docs/demo-output.log。
#
# 用法：./demo.sh [端口]；默认端口 0 = 由操作系统分配空闲端口（避免冲突）。
set -uo pipefail
cd "$(dirname "$0")"

PORT="${1:-0}"

./build.sh >/dev/null

# 关键参数：允许乱序 100ms，历史版本额外保留 5000ms
java -cp build/classes drvb.Main \
  --port="$PORT" --clock=manual --start-time=90000 \
  --allowed-lateness=100 --retention-horizon=5000 \
  > build/demo-server.log 2>&1 &
SERVER_PID=$!

cleanup() { kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT

# 从启动日志解析实际监听端口（port=0 时由系统分配）
ACTUAL_PORT=""
for _ in $(seq 1 100); do
  ACTUAL_PORT=$(grep -oE '127\.0\.0\.1:[0-9]+' build/demo-server.log | head -1 | cut -d: -f2)
  [ -n "$ACTUAL_PORT" ] && break
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "服务启动失败，日志：" >&2
    cat build/demo-server.log >&2
    exit 1
  fi
  sleep 0.1
done
PORT="$ACTUAL_PORT"
BASE="http://127.0.0.1:$PORT"

# 等待 /health 返回 JSON（避免误连到同端口的其他服务）
READY=""
for _ in $(seq 1 50); do
  READY=$(curl -s "$BASE/health" 2>/dev/null | grep -o '"status": "UP"' || true)
  [ -n "$READY" ] && break
  sleep 0.1
done
[ -z "$READY" ] && { echo "健康检查失败" >&2; cat build/demo-server.log >&2; exit 1; }
echo "# 服务已在 $BASE 就绪（manual 时钟）"

req() { # method path [json-body]
  local method="$1" path="$2" body="${3:-}"
  echo "------------------------------------------------------------"
  if [ -n "$body" ]; then
    echo "$ curl -X $method $BASE$path -d '$body'"
    curl -s -X "$method" "$BASE$path" \
      -H 'Content-Type: application/json' -d "$body"
  else
    echo "$ curl -X $method $BASE$path"
    curl -s -X "$method" "$BASE$path"
  fi
  echo
}

echo "############ 动态规则版本绑定 · 端到端演示 ############"
req GET  /health

echo; echo "### 1) 发布 v1：事件时间 [0,1000)，amount>=100"
req POST /rules/publish '{"versionId":"v1","name":"threshold-100","effectiveFrom":0,"note":"初版","predicate":{"op":"gte","field":"amount","value":100}}'

echo; echo "### 2) 热更新 v2：事件时间 [1000,3000)，amount>=200（收紧）"
req POST /rules/publish '{"versionId":"v2","name":"threshold-200","effectiveFrom":1000,"note":"收紧到200","predicate":{"op":"gte","field":"amount","value":200}}'

echo; echo "### 3) 乱序事件：先到 eventTime=2500（v2），再到晚到 eventTime=500（v1）"
req POST /events '{"eventId":"a","eventTime":2500,"type":"payment","payload":{"amount":150}}'
req POST /events '{"eventId":"late-v1","eventTime":500,"type":"payment","payload":{"amount":150}}'

echo; echo "### 4) 边界时刻：eventTime=1000 恰好属于 v2（左闭右开）"
req POST /events '{"eventId":"edge1000","eventTime":1000,"payload":{"amount":100}}'

echo; echo "### 5) 缺失版本：eventTime=-1 早于首边界 -> 拒绝，不套用最新"
req POST /events '{"eventId":"ancient","eventTime":-1,"payload":{"amount":999}}'

echo; echo "### 6) 回滚：自事件时间 3000 起重新使用 v1"
req POST /rules/rollback '{"versionId":"v1","effectiveFrom":3000,"note":"v2误杀，回滚"}'
req POST /events '{"eventId":"edge3000","eventTime":3000,"payload":{"amount":150}}'

echo; echo "### 7) 回收前提检查：v2 区间 [1000,3000)，结束于 3000"
req POST /reclaim '{"mode":"check","versionId":"v2"}'
echo "  -- 推进事件时间到 8000：WM=7900, gate=2900 < 3000，前提不满足"
req POST /events '{"eventId":"push8000","eventTime":8000,"payload":{"amount":1}}'
req POST /reclaim '{"mode":"check","versionId":"v2"}'
echo "  -- 推进到 8100：WM=8000, gate=3000 == 区间结束，前提满足"
req POST /events '{"eventId":"push8100","eventTime":8100,"payload":{"amount":1}}'
req POST /reclaim '{"mode":"check","versionId":"v2"}'

echo; echo "### 8) 回收前，v2 区间晚到事件仍用历史版本 v2"
req POST /events '{"eventId":"late-before-gc","eventTime":1500,"payload":{"amount":250}}'

echo; echo "### 9) 执行自动扫描回收（只回收满足前提的 v2）"
req POST /reclaim '{"mode":"eligible"}'

echo; echo "### 10) 回收后，同一历史区间更晚到达的事件被明确拒绝"
req POST /events '{"eventId":"late-after-gc","eventTime":1600,"payload":{"amount":250}}'

echo; echo "### 11) 当前区间仍由 v1 服务"
req POST /events '{"eventId":"cur","eventTime":9000,"payload":{"amount":150}}'

echo; echo "### 12) /admin/tick 推进可注入的处理时间"
req POST /admin/tick '{"advanceBy":5000}'

echo; echo "### 13) 状态总览 / 结果查询"
req GET  /state
req GET  "/results?status=REJECTED"

echo; echo "演示结束（服务将被关闭）"
