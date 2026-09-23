#!/usr/bin/env bash
# 端到端演示：启动服务 -> 后台订阅 SSE 事件 -> 并发提交若干请求
# （含部分失败与超大请求拒绝）-> 查看指标 -> 优雅关闭。
#
# 用法: bash examples/demo.sh
set -euo pipefail

HOSTPORT=${ADDR:-127.0.0.1:19090}
HOST=${HOSTPORT%%:*}
PORT=${HOSTPORT##*:}
BASE="http://${HOSTPORT}"

TMPDIR="$(mktemp -d)"
BIN="$TMPDIR/batchagg"
EVENTS_LOG="$TMPDIR/events.sse"
SERVER_LOG="$TMPDIR/server.log"
trap 'rm -rf "$TMPDIR"; kill ${SRV_PID:-} 2>/dev/null || true' EXIT

cd "$(dirname "$0")/.."
go build -o "$BIN" .

echo ">> 启动服务（max_count=4, max_wait=80ms, exec_latency=30ms）..."
"$BIN" \
  -addr "${HOST}:${PORT}" \
  -max-count 4 \
  -max-batch-bytes 4096 \
  -max-item-bytes 2048 \
  -max-wait 80ms \
  -exec-latency 30ms \
  >"$SERVER_LOG" 2>&1 &
SRV_PID=$!

# 等待健康检查通过。
healthy=false
for _ in $(seq 1 100); do
  if curl --noproxy "*" -fsS "$BASE/healthz" >/dev/null 2>&1; then healthy=true; break; fi
  if ! kill -0 "$SRV_PID" 2>/dev/null; then
    echo "!! 服务进程提前退出，日志如下："
    cat "$SERVER_LOG"
    exit 1
  fi
  sleep 0.05
done
if ! $healthy; then
  echo "!! 健康检查超时，日志如下："
  cat "$SERVER_LOG"
  exit 1
fi

echo ">> 后台订阅 SSE 事件（输出到 $EVENTS_LOG）..."
curl --noproxy "*" -fsS -N "$BASE/v1/events" >"$EVENTS_LOG" &
SSE_PID=$!
sleep 0.1

echo ">> 并发提交 6 个请求到同一模型（MaxCount=4，预期满批4 + 超时2，共两批）..."
pids=()
for i in 1 2 3 4 5 6; do
  curl --noproxy "*" -fsS -X POST "$BASE/v1/infer" \
    -H 'Content-Type: application/json' \
    -d "{\"id\":\"demo-$i\",\"model\":\"llm-demo-a\",\"prompt\":\"第 $i 条提示词\"}" \
    | python3 -c 'import sys,json; r=json.load(sys.stdin); print("  <-", r["request_id"], r["status"], r.get("result",{}).get("batch_seq"))' &
  pids+=($!)
done
wait "${pids[@]}" || true

echo
echo ">> 提交 1 个部分失败 + 1 个正常请求（同模型，超时同批）..."
curl --noproxy "*" -sS -X POST "$BASE/v1/infer" -H 'Content-Type: application/json' \
  -d @examples/request_partial_failure.json | python3 -m json.tool
curl --noproxy "*" -sS -X POST "$BASE/v1/infer" -H 'Content-Type: application/json' \
  -d @examples/request_basic.json | python3 -m json.tool

echo ">> 提交超大请求（预期 HTTP 413）..."
BIG=$(printf 'x%.0s' $(seq 1 5000))
curl --noproxy "*" -sS -o /tmp/batchagg-413.json -w 'HTTP %{http_code}\n' -X POST "$BASE/v1/infer" \
  -H 'Content-Type: application/json' \
  -d "{\"id\":\"too-big\",\"model\":\"llm-demo-a\",\"prompt\":\"$BIG\"}" || true
cat /tmp/batchagg-413.json | python3 -m json.tool

sleep 0.2
echo ">> 指标快照:"
curl --noproxy "*" -fsS "$BASE/v1/metrics" | python3 -m json.tool

kill $SSE_PID 2>/dev/null || true
echo
echo ">> 捕获到的 SSE 事件（前 24 行）:"
head -24 "$EVENTS_LOG" || true

echo ">> SIGTERM 优雅关闭 ..."
kill -TERM "$SRV_PID"
wait "$SRV_PID" 2>/dev/null || true
echo ">> 服务日志尾部:"
tail -4 "$SERVER_LOG"
