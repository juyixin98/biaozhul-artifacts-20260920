#!/usr/bin/env bash
# 请求样例：对本机运行的分词服务（默认 127.0.0.1:8080）发送 examples/ 下的请求。
# 用法：先启动 scripts/run-server.sh，再运行本脚本。可用 BASE_URL 覆盖地址。
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"

post() {
  local file="$1"
  echo "################################################################"
  echo "# POST /segment  <-  examples/$file"
  echo "################################################################"
  curl -s -w "\n[HTTP %{http_code}]\n" -X POST "$BASE_URL/segment" \
    -H 'Content-Type: application/json; charset=utf-8' \
    --data-binary @"examples/$file"
  echo
}

echo "===== GET /healthz ====="
curl -s -w "\n[HTTP %{http_code}]\n" "$BASE_URL/healthz"; echo

echo "===== GET /versions ====="
curl -s -w "\n[HTTP %{http_code}]\n" "$BASE_URL/versions"; echo

for f in 01-basic.json 02-nbest-v1.json 03-nbest-v2.json 04-unknown-chars.json 05-empty.json 06-error-version.json; do
  post "$f"
done
