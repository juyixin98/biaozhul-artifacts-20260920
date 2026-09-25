#!/usr/bin/env bash
# 对运行中的服务（默认 http://127.0.0.1:8080）逐个发送 examples/requests 下的请求。
# 用法：bash examples/curl-examples.sh [PORT]
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8080}"
BASE="http://127.0.0.1:${PORT}"

post() {
  local path="$1" file="$2"
  echo "### POST ${path}  <  ${file}"
  curl -s -X POST "${BASE}${path}" -H 'Content-Type: application/json' \
       --data-binary "@${file}"
  echo
}

echo "### GET /health"
curl -s "${BASE}/health"
echo
post /analyze requests/analyze-stopword.json
post /search  requests/repeated-word-echo.json
post /search  requests/cross-field-data-pipeline.json
post /search  requests/stopword-keeps-position.json
post /search  requests/greedy-trap-terms.json
post /search  requests/field-scoped-body.json
