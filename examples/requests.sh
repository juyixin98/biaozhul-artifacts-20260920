#!/usr/bin/env bash
# tztrigger HTTP 接口请求样例
# 前提：服务已启动（默认 :8080，可用 PORT 环境变量覆盖）
#   go run .            # 或 go build -o tztrigger . && ./tztrigger
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"

echo "== 健康检查 =="
curl -s "$BASE/healthz"

echo; echo "== 版本信息（含内嵌 tz 数据库版本与 SHA-256）=="
curl -s "$BASE/version"

echo; echo "== 创建触发器：纽约时间工作日 09:30 =="
curl -s -X POST "$BASE/triggers" -d '{
  "id": "weekday-morning",
  "minutes": "30",
  "hours": "9",
  "weekdays": "1-5",
  "timezone": "America/New_York",
  "catch_up_limit": 10
}'

echo; echo "== 创建触发器：上海时间每天 08:00（自动生成 ID）=="
curl -s -X POST "$BASE/triggers" -d '{
  "minutes": "0", "hours": "8", "weekdays": "*", "timezone": "Asia/Shanghai"
}'

echo; echo "== 列出全部触发器 =="
curl -s "$BASE/triggers"

echo; echo "== 预览 2026 秋季 DST 切换窗口（11-01 01:30 重复，只取较早一次）=="
curl -s -X POST "$BASE/triggers" -d '{
  "id": "dst-0130", "minutes": "30", "hours": "1",
  "weekdays": "*", "timezone": "America/New_York"
}' > /dev/null
curl -s "$BASE/triggers/dst-0130/preview?from=2026-10-31T00:00:00Z&to=2026-11-03T00:00:00Z"

echo; echo "== 预览 2026 春季 DST 切换窗口（03-08 02:30 缺失，跳过）=="
curl -s -X POST "$BASE/triggers" -d '{
  "id": "dst-0230", "minutes": "30", "hours": "2",
  "weekdays": "*", "timezone": "America/New_York"
}' > /dev/null
curl -s "$BASE/triggers/dst-0230/preview?from=2026-03-07T00:00:00Z&to=2026-03-10T00:00:00Z"

echo; echo "== 查询触发事件 =="
curl -s "$BASE/events?trigger_id=weekday-morning&limit=20"

echo; echo "== 删除触发器 =="
curl -s -X DELETE -o /dev/null -w "%{http_code}\n" "$BASE/triggers/dst-0230"

echo "== 错误样例：未知时区 → 400 =="
curl -s -X POST "$BASE/triggers" -d '{
  "minutes": "0", "hours": "9", "weekdays": "1", "timezone": "Mars/Olympus"
}'
echo
