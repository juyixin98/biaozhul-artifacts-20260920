#!/usr/bin/env bash
# 对运行中的服务（默认 127.0.0.1:8080）逐个发送 samples/ 下的请求并打印响应。
# 用法: ./samples/curl-demo.sh [baseUrl]
set -euo pipefail
BASE="${1:-http://127.0.0.1:8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# 绕过本机/环境里的 HTTP 代理（沙箱常见），仅影响本次命令
curl_args=(--noproxy '*' -sS -X POST "$BASE/evaluate" -H 'Content-Type: application/json')

pretty() { python3 -m json.tool --no-ensure-ascii 2>/dev/null || cat; }

for f in "$HERE"/0*.json; do
  echo "=============================================================="
  echo "POST $BASE/evaluate  <  $(basename "$f")"
  echo "--------------------------------------------------------------"
  curl "${curl_args[@]}" --data @"$f" | pretty
  echo
done

echo "=============================================================="
echo "有状态会话：创建 -> 追加事件 -> 水位 -> flush -> replay"
echo "--------------------------------------------------------------"
SID=$(curl --noproxy '*' -sS -X POST "$BASE/api/sessions" \
  -H 'Content-Type: application/json' \
  -d '{"a":"A","b":"B","c":"C","windowMs":1000,"policy":"ALL_PAIRS"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["sessionId"])')
echo "sessionId = $SID"
curl --noproxy '*' -sS -X POST "$BASE/api/sessions/$SID/events" \
  -H 'Content-Type: application/json' \
  -d '{"events":[{"id":"a1","key":"A","timestamp":0},{"id":"a2","key":"A","timestamp":100},{"id":"b1","key":"B","timestamp":400}]}' | pretty
curl --noproxy '*' -sS -X POST "$BASE/api/sessions/$SID/watermark?watermark=401" \
  -H 'Content-Type: application/json' -d '{}' | pretty
echo "-- flush --"
curl --noproxy '*' -sS -X POST "$BASE/api/sessions/$SID/flush" \
  -H 'Content-Type: application/json' -d '{}' | pretty
echo "-- replay（reset 后重放全部喂入事件）--"
curl --noproxy '*' -sS -X POST "$BASE/api/sessions/$SID/replay" \
  -H 'Content-Type: application/json' -d '{}' | pretty
