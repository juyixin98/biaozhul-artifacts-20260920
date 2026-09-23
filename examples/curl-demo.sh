#!/usr/bin/env bash
# 端到端演示：建租户 → 并发/超额预留 → 提交 → 取消 → 用量查询。
# 用法：先启动服务（cargo run --release），再 bash examples/curl-demo.sh
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8080}
T=${T:-demo-$RANDOM}

j() {
  local input
  input=$(cat)
  echo "$input" | python3 -m json.tool 2>/dev/null || echo "$input"
}

echo "== 创建租户 $T（1000 字节 / 10 对象） =="
curl -sS -X PUT "$BASE/tenants/$T" -H 'content-type: application/json' \
  -d '{"byte_quota":1000,"object_quota":10}' -o /dev/null -w 'PUT  -> %{http_code}\n'

echo "== 预留 600 字节 / 6 对象（TTL 60s） =="
RESP=$(curl -sS -X POST "$BASE/tenants/$T/reservations" -H 'content-type: application/json' \
  -d '{"byte_size":600,"object_count":6,"ttl_ms":60000}')
echo "$RESP" | j
ID=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["reservation_id"])')

echo "== 再预留 500/5：应 409 quota_exceeded =="
curl -sS -X POST "$BASE/tenants/$T/reservations" -H 'content-type: application/json' \
  -d '{"byte_size":500,"object_count":5,"ttl_ms":60000}' -w '\nHTTP %{http_code}\n' | j

echo "== 提交第一笔，转实占 =="
curl -sS -X POST "$BASE/reservations/$ID/commit" | j

echo "== 对已提交预留取消：应 409 =="
curl -sS -X POST "$BASE/reservations/$ID/cancel" -w '\nHTTP %{http_code}\n' | j

echo "== 新预留 400/4 后取消两次（幂等，不多释放） =="
RESP=$(curl -sS -X POST "$BASE/tenants/$T/reservations" -H 'content-type: application/json' \
  -d '{"byte_size":400,"object_count":4,"ttl_ms":60000}')
ID2=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["reservation_id"])')
curl -sS -X POST "$BASE/reservations/$ID2/cancel" | j
curl -sS -X POST "$BASE/reservations/$ID2/cancel" | j

echo "== 最终用量：应为 committed 600/6，预留 0 =="
curl -sS "$BASE/tenants/$T/usage" | j
