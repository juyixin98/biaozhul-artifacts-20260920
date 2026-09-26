#!/usr/bin/env bash
# 请求样例：先启动服务  go run ./cmd/server -addr 127.0.0.1:8080 -budget 4
set -u
BASE="${BASE:-http://127.0.0.1:8080}"

echo "== healthz =="
curl -s "$BASE/healthz"; echo

echo "== 1. 正常链路（幂等） =="
curl -s -X POST "$BASE/call" -H 'X-Idempotent: true' -w '\nHTTP %{http_code}\n'

echo "== 2. 瞬时故障：失败 2 次后恢复（重试成功） =="
curl -s -X POST "$BASE/call" \
  -H 'X-Idempotent: true' -H 'X-Fault-Id: demo1' -H 'X-Fault-Fail-Times: 2' \
  -w '\nHTTP %{http_code}\n'

echo "== 3. 持续故障：预算耗尽 -> 429 budget_exhausted =="
curl -s -X POST "$BASE/call" \
  -H 'X-Idempotent: true' -H 'X-Fault-Id: demo2' -H 'X-Fault-Fail-Times: 100' \
  -w '\nHTTP %{http_code}\n'

echo "== 4. 非幂等：不重试，直接 503 =="
curl -s -X POST "$BASE/call" \
  -H 'X-Idempotent: false' -H 'X-Fault-Id: demo3' -H 'X-Fault-Fail-Times: 100' \
  -w '\nHTTP %{http_code}\n'

echo "== 5. 服务端 Retry-After: 1 被遵守（总耗时约 1s） =="
curl -s -X POST "$BASE/call" \
  -H 'X-Idempotent: true' -H 'X-Fault-Id: demo4' -H 'X-Fault-Fail-Times: 1' \
  -H 'X-Fault-Retry-After: 1' -w '\nHTTP %{http_code} (%{time_total}s)\n'

echo "== 6. 自带更小根预算（2）与截止时间 =="
DEADLINE=$(( $(date +%s%3N) + 5000 ))
curl -s -X POST "$BASE/call" \
  -H 'X-Idempotent: true' -H 'X-Retry-Budget-Remaining: 2' \
  -H "X-Deadline-Unix-Milli: $DEADLINE" \
  -H 'X-Fault-Id: demo5' -H 'X-Fault-Fail-Times: 100' \
  -w '\nHTTP %{http_code}\n'
