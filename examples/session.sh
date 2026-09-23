#!/usr/bin/env bash
# 端到端请求样例：对本地服务依次执行插入/查询/撤回/滑出，并打印每一步响应。
# 前置：scripts/run.sh 18080 10000  （窗口 10000ms）
# 也可直接执行本脚本，它会自己拉起服务、跑完后关闭。
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-18080}"
BASE="http://127.0.0.1:${PORT}"
WINDOW=10000

if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
  JAVA="$JAVA_HOME/bin/java"
elif command -v java >/dev/null 2>&1; then
  JAVA="java"
elif [ -x "$HOME/jdk21/usr/lib/jvm/java-21-openjdk-amd64/bin/java" ]; then
  JAVA="$HOME/jdk21/usr/lib/jvm/java-21-openjdk-amd64/bin/java"
else
  echo "错误: 找不到 java" >&2; exit 1
fi

STARTED_HERE=0
if ! curl -s -o /dev/null "${BASE}/health"; then
  [ -d build/classes ] || scripts/build.sh
  "$JAVA" -cp build/classes topk.TopKHttpServer "$PORT" "$WINDOW" >/tmp/topk-example.log 2>&1 &
  SRV_PID=$!
  STARTED_HERE=1
  for _ in $(seq 1 50); do
    curl -s -o /dev/null "${BASE}/health" && break
    sleep 0.1
  done
fi

pretty() { if command -v python3 >/dev/null 2>&1; then python3 -m json.tool; else cat; fi; }

req() { # 方法 路径 JSON体(可空) 查询串(可空)
  local method="$1" path="$2" body="${3:-}" query="${4:-}"
  echo "────────────────────────────────────────"
  echo "\$ curl -X $method ${BASE}${path}${query} ${body:+--data '$body'}"
  if [ -n "$body" ]; then
    curl -s -X "$method" -H 'Content-Type: application/json' --data "$body" "${BASE}${path}${query}" | pretty
  else
    curl -s -X "$method" "${BASE}${path}${query}" | pretty
  fi
}

G=/groups/leaderboard

echo "### 1) 健康检查"
req GET /health

echo "### 2) 插入事件（窗口 10000ms，全部使用显式逻辑时间戳 ts）"
req POST "$G/events" '{"eventId":"e1","itemId":"bob","delta":5,"ts":1000}'
req POST "$G/events" '{"eventId":"e2","itemId":"ada","delta":5,"ts":1001}'
req POST "$G/events" '{"eventId":"e3","itemId":"cyb","delta":5,"ts":1002}'
req POST "$G/events" '{"eventId":"e4","itemId":"dan","delta":8,"ts":1003}'

echo "### 3) 查询 Top2（dan=8 第一；ada/bob/cyb 同为 5，按 ID 字典序 ada 第二）"
req GET "$G/topk" '' '?k=2&ts=2000'

echo "### 4) 负增量：ada 再发一条 delta=-9 -> ada=-4 垫底；K=100 大于元素数，返回全部"
req POST "$G/events" '{"eventId":"e5","itemId":"ada","delta":-9,"ts":2001}'
req GET "$G/topk" '' '?k=100&ts=3000'

echo "### 5) 撤回 e5（ada 恢复 5 分）"
req POST "$G/retract" '{"eventId":"e5","ts":4000}'
echo "### 5b) 重复撤回：幂等，返回 ALREADY_RETRACTED，不二次扣减"
req POST "$G/retract" '{"eventId":"e5","ts":4001}'
req GET "$G/topk" '' '?k=10&ts=4002'

echo "### 6) 窗口滑出：水位推到 ts=11000，左边界=1000，e1(bob,ts=1000) 恰好滑出"
req GET "$G/topk" '' '?k=10&ts=11000'

echo "### 7) 迟到事件被拒（422 LATE）；重复 eventId 被拒（409 DUPLICATE）"
req POST "$G/events" '{"eventId":"late","itemId":"x","delta":1,"ts":1}'
req POST "$G/events" '{"eventId":"e2","itemId":"ada","delta":5,"ts":2000}'

echo "### 8) 撤回已过期事件（404 EVENT_UNKNOWN）"
req POST "$G/retract" '{"eventId":"e1","ts":11000}'

echo "### 9) 内部快照（完整排序 + 计数，便于排障）"
req GET "$G/snapshot"

if [ "$STARTED_HERE" -eq 1 ]; then
  kill "$SRV_PID" 2>/dev/null || true
fi
