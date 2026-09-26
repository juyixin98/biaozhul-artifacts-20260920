#!/usr/bin/env bash
# 持久化恢复验收：单调读数不落盘；重启后单调钟归零，仅按墙钟截止时间与当前墙钟重新计算。
set -u
BASE="${1:-http://localhost:8088}"
hr(){ printf '\n========== %s ==========\n' "$1"; }

hr "1) 安排一个 1 小时超时，并让单调钟真实流逝 10 分钟"
curl -s -X POST "$BASE/timeouts" -H 'Content-Type: application/json' \
  -d '{"id":"restart-job","label":"restart demo","rule":{"type":"duration","seconds":3600}}' | jq -c '.data | {id,deadlineWall,fireMonoNanos}'
curl -s -X POST "$BASE/clock/tick" -H 'Content-Type: application/json' -d '{"seconds":600}' | jq -c '.data | {wallNow,monoNowNanos}'
curl -s "$BASE/timeouts/restart-job" | jq -c '.data | {id,status,remainingMonotonicNanos}'

hr "2) 落盘内容：只有墙钟截止时间，没有任何单调读数（grep fireMono 应为空）"
echo "--- data/timeouts.json (restart-job 条目) ---"
jq '.[] | select(.id=="restart-job")' "${2:-data/timeouts.json}"
echo "--- 文件中出现 fireMono / monoNanos 的次数（期望 0）---"
grep -c -iE 'fireMono|monoNanos' "${2:-data/timeouts.json}" || true

hr "3) 模拟重启，且重启瞬间墙钟被 NTP 向前拨到 12:00（截止 11:00 已过）"
curl -s -X POST "$BASE/admin/restart" -H 'Content-Type: application/json' \
  -d '{"wallNow":"2026-09-25T12:00:00Z"}' \
  | jq -c '.data | {generation,wallNow,monoNowNanos, note, restartJob:(.timeouts[]|select(.id=="restart-job")|{id,status,deadlineWall,fireMonoNanos,remainingMonotonicNanos})}'

hr "4) 再演示一次：未到期重启，剩余时间按墙钟差重新换算（而非沿用旧进程值）"
curl -s -X POST "$BASE/timeouts" -H 'Content-Type: application/json' \
  -d '{"id":"fresh-10m","rule":{"type":"duration","seconds":600}}' >/dev/null
# 重启时墙钟只前进 100 秒：旧进程若仍活着剩余应是 500s；新进程重新换算也必须是 500s
curl -s -X POST "$BASE/admin/restart" -H 'Content-Type: application/json' \
  -d '{"wallNow":"2026-09-25T12:01:40Z"}' \
  | jq -c '.data | {generation,monoNowNanos, fresh:(.timeouts[]|select(.id=="fresh-10m")|{status,deadlineWall,remainingMonotonicNanos})}'
