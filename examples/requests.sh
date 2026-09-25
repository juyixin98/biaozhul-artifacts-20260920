#!/usr/bin/env bash
# 资源预约服务请求样例。
#
# 用法：
#   1) 先启动服务（假时钟固定在 2030-01-01，便于演示）：
#        go run ./cmd/reservations -addr 127.0.0.1:8080 \
#          -fake-clock 2030-01-01T00:00:00Z -seed examples/seed.json
#   2) 另开终端执行：bash examples/requests.sh
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8080}
# 美化 JSON 且保留中文（ensure_ascii=False）。
j() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))'; }

# show METHOD PATH DATA：发请求，先打印状态行再美化 JSON 体。
show() {
  local method=$1 path=$2 data=$3
  if [[ -n $data ]]; then
    curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' \
      -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$data"
  else
    curl -s -o /tmp/resp.json -w 'HTTP %{http_code}\n' -X "$method" "$BASE$path"
  fi
  j < /tmp/resp.json
}

echo "== 0. 健康检查 =="
curl -s "$BASE/healthz" | j

echo "== 1. 查看已有资源 =="
curl -s "$BASE/v1/resources" | j

echo "== 2. 新建多维容量资源（机房：3 台机器 / 12 核） =="
curl -s -X POST "$BASE/v1/resources" \
  -H 'Content-Type: application/json' \
  -d '{"id":"lab","name":"计算机房","capacity":[3,12]}' | j

echo "== 3. 固定区间预约（与 seed-meeting 相邻，应成功） =="
curl -s -X POST "$BASE/v1/reservations" \
  -H 'Content-Type: application/json' \
  -d '{
        "id": "adjacent-ok",
        "resource": "room-a",
        "interval": {"start": "2030-01-01T10:00:00Z", "end": "2030-01-01T11:00:00Z"},
        "demand": [1, 0]
      }' | j

echo "== 4. 重叠区间预约（应 409 冲突，结构化错误带码） =="
show POST /v1/reservations '{
        "resource": "room-a",
        "interval": {"start": "2030-01-01T09:30:00Z", "end": "2030-01-01T10:30:00Z"},
        "demand": [1, 0]
      }' 

echo "== 5. 零容量维度上的正需求（应 400） =="
show POST /v1/reservations '{
        "resource": "room-a",
        "interval": {"start": "2030-01-01T08:00:00Z", "end": "2030-01-01T08:30:00Z"},
        "demand": [0, 1]
      }' 

echo "== 6. 最早可行位置查询（不落地） =="
curl -s -X POST "$BASE/v1/reservations:earliest-feasible" \
  -H 'Content-Type: application/json' \
  -d '{
        "resource": "room-a",
        "window": {"start": "2030-01-01T08:00:00Z", "end": "2030-01-01T13:00:00Z"},
        "duration_minutes": 120,
        "demand": [1, 0]
      }' | j

echo "== 7. 原子批量：两笔都可行（相邻打满 + 自动放置） =="
curl -s -X POST "$BASE/v1/reservations:batch" \
  -H 'Content-Type: application/json' \
  -d '{
        "items": [
          {"fixed": {
            "id": "batch-1", "resource": "lab",
            "interval": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-01T02:00:00Z"},
            "demand": [2, 8]
          }},
          {"place": {
            "id": "batch-2", "resource": "lab",
            "window": {"start": "2030-01-01T00:00:00Z", "end": "2030-01-01T06:00:00Z"},
            "duration_minutes": 120,
            "demand": [2, 8]
          }}
        ]
      }' | j

echo "== 8. 原子批量：一笔冲突 -> 整批拒绝（注意 batch-bad-* 均不存在） =="
show POST /v1/reservations:batch '{
        "items": [
          {"fixed": {
            "id": "batch-bad-good", "resource": "room-a",
            "interval": {"start": "2030-01-01T11:00:00Z", "end": "2030-01-01T12:00:00Z"},
            "demand": [1, 0]
          }},
          {"fixed": {
            "id": "batch-bad-conflict", "resource": "room-a",
            "interval": {"start": "2030-01-01T09:00:00Z", "end": "2030-01-01T09:30:00Z"},
            "demand": [1, 0]
          }}
        ]
      }' 
echo "-- 确认 batch-bad-good 未落地（应 404） --"
curl -s -o /dev/null -w 'GET batch-bad-good -> HTTP %{http_code}\n' \
  "$BASE/v1/reservations/batch-bad-good"

echo "== 9. 取消预约并立即复用其区间 =="
curl -s -X DELETE "$BASE/v1/reservations/seed-meeting" | j
curl -s -X POST "$BASE/v1/reservations" \
  -H 'Content-Type: application/json' \
  -d '{
        "id": "reused-slot",
        "resource": "room-a",
        "interval": {"start": "2030-01-01T09:00:00Z", "end": "2030-01-01T10:00:00Z"},
        "demand": [1, 0]
      }' | j

echo "== 10. 查询结构化事件流 =="
curl -s "$BASE/v1/events" | j
