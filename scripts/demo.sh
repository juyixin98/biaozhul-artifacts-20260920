#!/usr/bin/env bash
# 演示脚本：两事务环状等待 -> 死锁检测 -> 中止年轻事务 -> 重放完成 -> 输出报告
# 前提：服务已启动（默认 http://127.0.0.1:3000，可用 BASE 环境变量覆盖）
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:3000}

txid() { python3 -c 'import sys,json;print(json.load(sys.stdin)["tx_id"])'; }

echo "== begin T1, T2"
T1=$(curl -s -X POST "$BASE/tx" | txid)
T2=$(curl -s -X POST "$BASE/tx" | txid)
echo "T1=$T1 T2=$T2"

echo "== T1 locks A (exclusive), T2 locks B (exclusive)"
curl -s -X POST "$BASE/tx/$T1/locks" -H 'content-type: application/json' \
  -d '{"resource":"A","mode":"exclusive"}'; echo
curl -s -X POST "$BASE/tx/$T2/locks" -H 'content-type: application/json' \
  -d '{"resource":"B","mode":"exclusive"}'; echo

echo "== T1 requests B in background (waits)"
TMP=$(mktemp)
(curl -s -X POST "$BASE/tx/$T1/locks" -H 'content-type: application/json' \
  -d '{"resource":"B","mode":"exclusive","timeout_ms":10000}' > "$TMP") &
sleep 0.5

echo "== wait-for graph (expect edge T1 -> T2)"
curl -s "$BASE/graph"; echo

echo "== T2 requests A -> cycle; T2 (younger, larger txid) is the victim"
curl -s -X POST "$BASE/tx/$T2/locks" -H 'content-type: application/json' \
  -d '{"resource":"A","mode":"exclusive","timeout_ms":10000}'; echo
sleep 0.5

echo "== T1's pending request is now granted"
cat "$TMP"; echo

echo "== T1 commits"
curl -s -X POST "$BASE/tx/$T1/commit"; echo

echo "== replay aborted transaction as T3: lock A, lock B, commit"
T3=$(curl -s -X POST "$BASE/tx" | txid)
curl -s -X POST "$BASE/tx/$T3/locks" -H 'content-type: application/json' \
  -d '{"resource":"A","mode":"exclusive"}'; echo
curl -s -X POST "$BASE/tx/$T3/locks" -H 'content-type: application/json' \
  -d '{"resource":"B","mode":"exclusive"}'; echo
curl -s -X POST "$BASE/tx/$T3/commit"; echo

echo "== final report"
curl -s "$BASE/report"
