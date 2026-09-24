#!/usr/bin/env bash
# 端到端示例：启动服务 -> 打 6 个样例请求（含一个预期 422 类型错误）-> 关闭。
# 用法：./examples/curl-demo.sh [port]
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-8080}"
BASE="http://127.0.0.1:$PORT"

./build.sh
TVL_PORT="$PORT" java -cp build/classes com.tvl.Main >/tmp/tvl-demo.log 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

# 等服务起来
for _ in $(seq 1 50); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

post() {
  local f="$1"
  echo "================  $f  ================"
  curl -s -w "\n[HTTP %{http_code}]\n" -H 'Content-Type: application/json' \
       -X POST "$BASE/query" --data-binary @"$f"
  echo
}

echo "################  GET /health  ################"
curl -s -w "\n[HTTP %{http_code}]\n" "$BASE/health"; echo

post examples/01-basic-3vl.json
post examples/02-null-bitmap-and-not.json
post examples/03-parentheses-precedence.json
post examples/04-parameter-binding.json
post examples/05-type-error-string-to-int.json
post examples/06-empty-batch.json

echo "演示完成。服务日志：/tmp/tvl-demo.log"
