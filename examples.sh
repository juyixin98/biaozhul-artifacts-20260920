#!/usr/bin/env bash
# tztrig HTTP API 请求样例。
# 用法：
#   ./bin/tztrig -addr=:18080 -state=data/state.json -catchup=100 &
#   ./examples.sh
set -u
BASE="${BASE:-http://127.0.0.1:8080}"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 1. 健康检查 =="
curl -s "$BASE/api/health"; echo

echo "== 2. 服务信息（固定 tzdata 版本 / 补触发上限）=="
curl -s "$BASE/api/info"; echo

echo "== 3. 查找可用时区（前缀过滤）=="
curl -s "$BASE/api/zones?prefix=America/New"; echo

echo "== 4. 创建：纽约每天本地 02:30（会在春切跳过、秋切取较早一次）=="
curl -s -X POST "$BASE/api/schedules" \
  -H 'Content-Type: application/json' \
  -d '{"id":"nightly-ny","minute":"30","hour":"2","weekday":"*","timezone":"America/New_York"}' | j; echo

echo "== 5. 创建：柏林工作日 09:00 =="
curl -s -X POST "$BASE/api/schedules" \
  -H 'Content-Type: application/json' \
  -d '{"id":"work-berlin","minute":"0","hour":"9","weekday":"1-5","timezone":"Europe/Berlin"}' | j; echo

echo "== 6. 创建：UTC 每 15 分钟 =="
curl -s -X POST "$BASE/api/schedules" \
  -H 'Content-Type: application/json' \
  -d '{"id":"qhour","minute":"*/15","hour":"*","weekday":"*","timezone":"UTC"}' | j; echo

echo "== 7. 非法表达式（minute=99）应返回 400 =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/api/schedules" \
  -H 'Content-Type: application/json' \
  -d '{"id":"bad","minute":"99","hour":"0","weekday":"*","timezone":"UTC"}'

echo "== 8. 非法时区应返回 400 =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/api/schedules" \
  -H 'Content-Type: application/json' \
  -d '{"id":"bad2","minute":"0","hour":"0","weekday":"*","timezone":"Mars/Olympus"}'

echo "== 9. 列出全部计划 =="
curl -s "$BASE/api/schedules" | j; echo

echo "== 10. 预览 nightly-ny 未来 5 次触发（注意春切/秋切的偏移与跳过）=="
curl -s "$BASE/api/schedules/nightly-ny/next?n=5" | j; echo

echo "== 11. 暂停 / 恢复 =="
curl -s -X POST "$BASE/api/schedules/qhour/pause" | j; echo
curl -s -X POST "$BASE/api/schedules/qhour/resume"  | j; echo

echo "== 12. 查询单个计划（含 lastFired 水位线）=="
curl -s "$BASE/api/schedules/nightly-ny" | j; echo

echo "== 13. 最近已投递触发 =="
curl -s "$BASE/api/fires?limit=10" | j; echo

echo "== 14. 最近一次补触发被上限丢弃的区间（无停机时为空数组）=="
curl -s "$BASE/api/skips" | j; echo

echo "== 15. 删除计划（204）=="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X DELETE "$BASE/api/schedules/work-berlin"
