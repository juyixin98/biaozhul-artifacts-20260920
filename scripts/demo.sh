#!/usr/bin/env bash
# 端到端演示：真实 HTTP 双组件 + 真实时钟（短 TTL，只需等待几秒）。
# 场景与集成测试一致，但走 curl 打正在运行的 server。
set -euo pipefail

LOCK="${LOCK_URL:-http://127.0.0.1:18080}"
RES="${RES_URL:-http://127.0.0.1:18081}"
TTL_MS=3000

j() { python3 -m json.tool; }

echo "== 1) 旧持有者 A 获取锁（令牌 1） =="
A_RESP=$(curl -sS -X POST "$LOCK/v1/locks/config/acquire" \
  -H 'Content-Type: application/json' \
  -d "{\"holder\":\"holder-A\",\"ttl_ms\":$TTL_MS}")
echo "$A_RESP" | j
TOKEN_A=$(echo "$A_RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

echo "== 2) 租约有效时 B 抢锁（预期 409） =="
curl -sS -o /tmp/b409.json -w 'HTTP %{http_code}\n' -X POST "$LOCK/v1/locks/config/acquire" \
  -H 'Content-Type: application/json' \
  -d "{\"holder\":\"holder-B\",\"ttl_ms\":$TTL_MS}"
cat /tmp/b409.json | j

echo "== 3) 暂停旧持有者（不续租），等待租约过期 =="
sleep $((TTL_MS / 1000 + 1))

echo "== 4) B 获取锁，拿到递增令牌（预期 2） =="
B_RESP=$(curl -sS -X POST "$LOCK/v1/locks/config/acquire" \
  -H 'Content-Type: application/json' \
  -d "{\"holder\":\"holder-B\",\"ttl_ms\":$((TTL_MS * 2))}")
echo "$B_RESP" | j
TOKEN_B=$(echo "$B_RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

echo "== 5) 新持有者 B 写入资源（预期 200） =="
curl -sS -w '\nHTTP %{http_code}\n' -X PUT "$RES/v1/resources/config" \
  -H 'Content-Type: application/json' \
  -d "{\"token\":$TOKEN_B,\"value\":\"value-from-B\"}" | sed 's/^/  /'

echo "== 6) A 恢复，用旧令牌 $TOKEN_A 迟到写入（预期 409，被拒绝） =="
curl -sS -w '\nHTTP %{http_code}\n' -X PUT "$RES/v1/resources/config" \
  -H 'Content-Type: application/json' \
  -d "{\"token\":$TOKEN_A,\"value\":\"value-from-A-LATE\"}" | sed 's/^/  /'

echo "== 7) 资源内容仍是 B 的 =="
curl -sS "$RES/v1/resources/config" | j

echo "== 8) 锁服务当前计数（预期 2） =="
curl -sS "$LOCK/v1/counter" | j
