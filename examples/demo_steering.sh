#!/usr/bin/env bash
# 端到端验收演示：墙钟截止 -> 单调计时器；向前/向后校时不影响已安排超时；真实流逝才到期。
set -u
BASE="${1:-http://localhost:8088}"
hr(){ printf '\n========== %s ==========\n' "$1"; }
status(){ curl -s "$BASE/timeouts/$1" | jq -r '.data | "\(.id) status=\(.status) remainingMonoNs=\(.remainingMonotonicNanos) wallDeadline=\(.deadlineWall)"'; }

hr "1) 安排一个 600 秒（10 分钟）的超时"
curl -s -X POST "$BASE/timeouts" -H 'Content-Type: application/json' \
  -d '{"id":"demo-job","label":"acceptance demo","rule":{"type":"duration","seconds":600}}' | jq -c '.data | {id,status,deadlineWall,fireMonoNanos,remainingAtConversionNanos}'

hr "2) 向前校时 +3 小时（NTP fast）：墙钟变，单调读数与超时不变"
curl -s -X POST "$BASE/clock/set-wall" -H 'Content-Type: application/json' \
  -d '{"wall":"2026-09-25T13:00:00Z"}' | jq -c '.data | {wallNow,monoNowNanos,expiredIds}'
status demo-job

hr "3) 向后校时 -5 小时（NTP slow）：仍不改变已安排超时"
curl -s -X POST "$BASE/clock/advance-wall" -H 'Content-Type: application/json' \
  -d '{"seconds":-18000}' | jq -c '.data | {wallNow,monoNowNanos,expiredIds}'
status demo-job

hr "4) 真实流逝 599 秒（tick：墙钟与单调钟同步前进）"
curl -s -X POST "$BASE/clock/tick" -H 'Content-Type: application/json' -d '{"seconds":599}' | jq -c '.data | {wallNow,monoNowNanos}'
status demo-job

hr "5) 再流逝 1 秒 -> 在预定单调时刻到期（与墙钟被拨过无关）"
curl -s -X POST "$BASE/clock/tick" -H 'Content-Type: application/json' -d '{"seconds":1}' | jq -c '.data | {wallNow,monoNowNanos,expiredIds}'
status demo-job
curl -s "$BASE/timeouts/demo-job" | jq -c '.data | {id,status,firedAtWall,firedAtMonoNanos,fireMonoNanos}'

hr "6) 已过期截止：安排一个截止时间在过去的超时，立即 EXPIRED"
curl -s -X POST "$BASE/timeouts" -H 'Content-Type: application/json' \
  -d '{"id":"demo-past","rule":{"type":"absolute","deadline":"2026-09-25T05:00:00Z"}}' \
  | jq -c '.data | {id,status,remainingAtConversionNanos}'
