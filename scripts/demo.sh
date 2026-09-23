#!/usr/bin/env bash
# 端到端演示：摄取示例事件 -> 推进链高 -> 对账 -> 查看快照与异常。
# 前提：服务已在 $BASE（默认 http://127.0.0.1:8000）运行。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
cd "$(dirname "$0")/.."

echo "== 1. 摄取正常生命周期事件（重复投递一次验证幂等） =="
curl -s -X POST "$BASE/events/batch" -H 'Content-Type: application/json' \
    -d @examples/events_happy_path.json | python3 -m json.tool
curl -s -X POST "$BASE/events/batch" -H 'Content-Type: application/json' \
    -d @examples/events_happy_path.json | python3 -m json.tool

echo "== 2. 摄取异常事件（重复铸造 / 无锁定铸造 / 超时未配对） =="
curl -s -X POST "$BASE/events/batch" -H 'Content-Type: application/json' \
    -d @examples/events_anomalies.json | python3 -m json.tool

echo "== 3. 设置两条链的高度与确认数 =="
curl -s -X PUT "$BASE/chains/chainA/head" -H 'Content-Type: application/json' \
    -d '{"height": 1000, "confirmations": 12}' | python3 -m json.tool
curl -s -X PUT "$BASE/chains/chainB/head" -H 'Content-Type: application/json' \
    -d '{"height": 1000, "confirmations": 12}' | python3 -m json.tool

echo "== 4. 执行对账（配对超时 100 块） =="
curl -s -X POST "$BASE/reconcile" -H 'Content-Type: application/json' \
    -d '{"pairing_timeout_blocks": 100}' | python3 -m json.tool

echo "== 5. 查看最新快照的异常与证据路径 =="
SNAP=$(curl -s "$BASE/snapshots" | python3 -c "import json,sys; print(json.load(sys.stdin)[-1]['id'])")
curl -s "$BASE/snapshots/$SNAP" | python3 -m json.tool

echo "== 6. 当前各资产守恒视图 =="
curl -s "$BASE/assets" | python3 -m json.tool
