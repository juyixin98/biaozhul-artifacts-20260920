#!/usr/bin/env bash
# 请求样例：覆盖并列名次、负增量、K 大于元素数、窗口滑出、撤回。
# 前置：服务已在 $BASE（默认 http://127.0.0.1:8080）运行，windowMs=1000。
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"

echo "== 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 插入事件（含并列分数与负增量） =="
curl -s -X POST "$BASE/events" -d '{"eventId":"e1","group":"g1","key":"apple","delta":5,"ts":1000}'; echo
curl -s -X POST "$BASE/events" -d '{"eventId":"e2","group":"g1","key":"banana","delta":5,"ts":1000}'; echo
curl -s -X POST "$BASE/events" -d '{"eventId":"e3","group":"g1","key":"cherry","delta":9,"ts":1100}'; echo
curl -s -X POST "$BASE/events" -d '{"eventId":"e4","group":"g1","key":"apple","delta":-2,"ts":1200}'; echo

echo "== 重复 eventId（应 409） =="
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/events" -d '{"eventId":"e1","group":"g1","key":"x","delta":1,"ts":1300}'

echo "== TopK（K 大于元素数，返回全部；并列按 key 升序） =="
curl -s "$BASE/topk?group=g1&k=10&now=1500"; echo

echo "== 窗口内完整排序 =="
curl -s "$BASE/ranking?group=g1&now=1500"; echo

echo "== 撤回 e3（cherry -9） =="
curl -s -X POST "$BASE/retract" -d '{"eventId":"e3"}'; echo
curl -s "$BASE/ranking?group=g1&now=1500"; echo

echo "== 重复撤回（幂等空操作） =="
curl -s -X POST "$BASE/retract" -d '{"eventId":"e3"}'; echo

echo "== 推进时间使窗口滑出（now=2500，ts<=1500 的事件过期） =="
curl -s -X POST "$BASE/advance" -d '{"now":2500}'; echo
curl -s "$BASE/ranking?group=g1&now=2500"; echo

echo "== 过期后再撤回 e1（不得二次扣减） =="
curl -s -X POST "$BASE/retract" -d '{"eventId":"e1"}'; echo
curl -s "$BASE/ranking?group=g1&now=2500"; echo
