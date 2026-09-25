#!/usr/bin/env bash
# 针对本地 pitjoin 服务的请求样例（需先启动：python -m pitjoin.cli serve）
# 仅使用 curl，访问 127.0.0.1，无外部网络。
set -euo pipefail

BASE="${PITJOIN_BASE:-http://127.0.0.1:8000}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== 1) 健康检查 =="
curl -s "${BASE}/health"
echo

echo "== 2) 单点依据：晚到修订（E2 时刻 rA3 尚未入库，应选 rA2，剔除原因为 LATE_INGEST） =="
curl -s -X POST "${BASE}/explain" \
  -H 'Content-Type: application/json' \
  -d @"${DIR}/explain_late_revision.json"
echo

echo "== 3) 单点依据：同时间多版本（rB1/rB2/rB3 同时刻，按版本号取 rB3 v3=3.0） =="
curl -s -X POST "${BASE}/explain" \
  -H 'Content-Type: application/json' \
  -d @"${DIR}/explain_version_tie.json"
echo

echo "== 4) 批量连接：规范数据集 =="
curl -s -X POST "${BASE}/join" \
  -H 'Content-Type: application/json' \
  -d @"${DIR}/join_canonical.json"
echo

echo "== 5) 自带记录：晚到修订 + 完全重复记录 =="
curl -s -X POST "${BASE}/join" \
  -H 'Content-Type: application/json' \
  -d @"${DIR}/join_custom_late_and_duplicate.json"
echo

echo "== 6) 对照：use_event_as_of=false 朴素 join 会泄漏 rA3=111（仅用于演示，勿用于生产） =="
curl -s -X POST "${BASE}/join" \
  -H 'Content-Type: application/json' \
  -d '{"dataset":"canonical","use_event_as_of":false,"features":["f_balance"],
       "events":[{"entity_id":"cust_A","event_time":"2026-01-02T12:00:00Z"}]}'
echo
