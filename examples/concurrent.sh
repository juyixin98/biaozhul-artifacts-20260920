#!/usr/bin/env bash
# 并发样例：N 个请求在同一瞬间到达，验证全局+租户两层原子扣减、零超发。
#
# 前置：tokenbudgetd 已用 -virtual 启动（见 requests.sh 头部）。
set -u
B="${BASE_URL:-http://127.0.0.1:8099}"
N="${N:-50}"
TENANT="${TENANT:-shared}"

# 固定成两层都为 burst=7、速率 100/s 的小窗口，便于清点结果。
curl -s -X PUT "$B/v1/config" -H 'Content-Type: application/json' -d '{
  "global":  {"rate":"100","burst":"7"},
  "default": {"rate":"100","burst":"7"}
}' >/dev/null

echo "并发发送 $N 个 cost=1 请求到租户 $TENANT（两层 burst 均为 7）..."
seq 1 "$N" | xargs -P "$N" -I{} sh -c '
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "'"$B"'/v1/acquire" \
    -H "Content-Type: application/json" \
    -d "{\"tenant\":\"'"$TENANT"'\",\"cost\":\"1\"}"
' | sort | uniq -c

echo "请求后桶状态（global 与 tenant 都必须恰好为 0）："
curl -s "$B/v1/buckets/$TENANT" | python3 -m json.tool
