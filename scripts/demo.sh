#!/usr/bin/env bash
# 端到端演示：极小 series-budget + 合成高基数攻击 + 快照重启恢复。
# 用法: bash scripts/demo.sh
# 依赖: go, curl。脚本自建临时目录，退出时清理进程。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 动态选取空闲端口，避免与机器上已有服务冲突。
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
ADDR="127.0.0.1:$PORT"
WORK="$(mktemp -d)"
SNAP="$WORK/state.json"
BASE="http://$ADDR"

echo "== workdir: $WORK  addr: $ADDR"
trap 'kill "${SRV_PID:-0}" 2>/dev/null || true' EXIT

echo "== go build"
go build -o "$WORK/server" ./cmd/server
go build -o "$WORK/synthload" ./cmd/synthload

echo "== start server (series-budget=50, snapshot every 2s)"
"$WORK/server" \
  -addr="$ADDR" \
  -series-budget=50 \
  -max-metric-names=16 \
  -max-label-value-bytes=64 \
  -snapshot="$SNAP" \
  -flush-interval=2s &
SRV_PID=$!

for _ in $(seq 1 50); do
  if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

echo; echo "== ingest single sample"
curl -fsS -X POST "$BASE/ingest" -H 'Content-Type: application/json' \
  --data @examples/ingest-single.json; echo

echo; echo "== ingest batch"
curl -fsS -X POST "$BASE/ingest" -H 'Content-Type: application/json' \
  --data @examples/ingest-batch.json | head -c 400; echo

echo; echo "== run synthetic high-cardinality attack on 'attack_requests' (50 stable x3, 200000 unique)"
"$WORK/synthload" -addr="$ADDR" -metric=attack_requests -budget=50 -attack=200000 -batch=2000

echo; echo "== stats after attack (expect conservation_ok=true, overflowed=200000)"
curl -fsS "$BASE/stats" | python3 -m json.tool 2>/dev/null || curl -fsS "$BASE/stats"

echo; echo "== attack metric view: tracked_series must stay 50, overflow=200000"
curl -fsS "$BASE/metrics/attack_requests" | python3 -c '
import json,sys
v=json.load(sys.stdin)
print("series_budget:", v["series_budget"])
print("tracked_series:", v["tracked_series"])
print("overflow:", v["overflow"])
assert v["tracked_series"] == 50, "tracked series leaked!"
assert v["overflow"]["sample_count"] == 200000, "overflow count wrong!"
'

echo; echo "== edge cases (truncation + rejections, expect HTTP 202)"
curl -sS -o "$WORK/edge.json" -w "http_status=%{http_code}\n" -X POST "$BASE/ingest" \
  -H 'Content-Type: application/json' --data @examples/ingest-edge-cases.json
cat "$WORK/edge.json" | python3 -m json.tool 2>/dev/null || cat "$WORK/edge.json"

echo; echo "== force snapshot and stop server"
curl -fsS -X POST "$BASE/admin/snapshot"; echo
kill -TERM "$SRV_PID"
wait "$SRV_PID" 2>/dev/null || true
SRV_PID=""

echo; echo "== restart server from snapshot"
"$WORK/server" -addr="$ADDR" -series-budget=50 -max-metric-names=16 \
  -max-label-value-bytes=64 -snapshot="$SNAP" -flush-interval=60s &
SRV_PID=$!
for _ in $(seq 1 50); do
  if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

echo; echo "== stats after restart (counters must match pre-restart)"
curl -fsS "$BASE/stats" | python3 -m json.tool 2>/dev/null || curl -fsS "$BASE/stats"

echo; echo "== post-restart overflow still accumulates"
curl -fsS -X POST "$BASE/ingest" -H 'Content-Type: application/json' \
  -d '{"metric":"attack_requests","labels":{"request_id":"post-restart-evil"},"value":1}' | python3 -m json.tool 2>/dev/null || true

echo; echo "== demo finished; snapshot left at $SNAP"
