#!/usr/bin/env bash
# 端到端演示：启动服务 -> 造数（含边界与热键）-> 单侧水位（不得回收）
# -> 双侧水位（输出回收状态量）-> 迟到事件 -> 批量 actions -> 关停。
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-18123}"
BASE="http://127.0.0.1:$PORT"

[ -d target/classes ] || bash scripts/build.sh

if command -v python3 >/dev/null 2>&1; then
  pp() { python3 -m json.tool; }
else
  pp() { cat; }
fi

req() {  # req METHOD PATH [JSON_BODY]
  local method="$1" path="$2" body="${3:-}"
  echo "### $method $path"
  if [ -n "$body" ]; then
    curl -s -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body" | pp
  else
    curl -s -X "$method" "$BASE$path" | pp
  fi
  echo
}

cleanup() {
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null || true
}
trap cleanup EXIT

PORT="$PORT" BIND=127.0.0.1 "$(bash scripts/find-java.sh)/java" -cp target/classes ij.Main "$PORT" &
SRV_PID=$!

# 等待健康检查就绪（最多 10 秒）；本服务进程中途退出则立即失败。
for _ in $(seq 1 100); do
  if ! kill -0 "$SRV_PID" 2>/dev/null; then
    echo "ERROR: server process exited early (port $PORT occupied?)"; exit 1
  fi
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -sf "$BASE/health" >/dev/null 2>&1 || { echo "ERROR: server not healthy on port $PORT"; exit 1; }

echo "================ 1) 创建会话，区间 [-2, +2] ================"
req POST /sessions "$(cat examples/01-create-session.json)"

echo "================ 2) 灌入左流（普通 key + 热键 HOT） ================"
req POST /sessions/demo/events/left "$(cat examples/02-left-events.json)"

echo "================ 3) 灌入右流（含时间边界 ±2、超界 8） ================"
req POST /sessions/demo/events/right "$(cat examples/03-right-events.json)"

echo "================ 4) 查看全部配对（携带 payload） ================"
req GET "/sessions/demo/pairs?includePayload=true"

echo "================ 5) 仅推进左水位=100：单侧水位不得回收任何状态 ================"
req POST /sessions/demo/watermarks/left "$(cat examples/04-watermark-left.json)"

echo "================ 6) 推进右水位=100：双方水位齐备，输出被回收状态量 ================"
req POST /sessions/demo/watermarks/right "$(cat examples/05-watermark-right.json)"

echo "================ 7) 水位之后的迟到事件应被丢弃 ================"
req POST /sessions/demo/events/left '{"key":"u","ts":1,"id":"LATE1"}'

echo "================ 8) 最终 stats（回收累计量/丢弃量/配对量） ================"
req GET /sessions/demo/stats

echo "================ 9) /actions 批量编排（事件+水位顺序执行） ================"
req POST /sessions '{"sessionId":"batch","lowerBound":-1,"upperBound":1}'
req POST /sessions/batch/actions "$(cat examples/06-actions-batch.json)"

echo "demo finished."
