#!/usr/bin/env bash
# ehindex HTTP 接口请求示例。
# 用法：先启动服务（默认 127.0.0.1:8080），再运行本脚本：
#   cargo run -- --addr 127.0.0.1:8080 --file demo.db --cap 4 --hash fx
#   ./examples/requests.sh
set -u
BASE="${EHINDEX_BASE:-http://127.0.0.1:8080}"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 健康检查 =="
curl -s "$BASE/healthz"; echo

echo "== 插入/更新（upsert）=="
curl -s -X POST "$BASE/put" -H 'Content-Type: application/json' \
  -d '{"key":"name","value":"alice"}' | j; echo
curl -s -X POST "$BASE/put" -H 'Content-Type: application/json' \
  -d '{"key":"city","value":"shanghai"}' | j; echo

echo "== 查询 =="
curl -s -X POST "$BASE/get" -H 'Content-Type: application/json' \
  -d '{"key":"name"}' | j; echo

echo "== 更新已有键 =="
curl -s -X POST "$BASE/put" -H 'Content-Type: application/json' \
  -d '{"key":"name","value":"bob"}' >/dev/null
curl -s -X POST "$BASE/get" -H 'Content-Type: application/json' \
  -d '{"key":"name"}' | j; echo

echo "== 查询不存在的键（404）=="
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/get" \
  -H 'Content-Type: application/json' -d '{"key":"ghost"}'

echo "== 删除 =="
curl -s -X POST "$BASE/delete" -H 'Content-Type: application/json' \
  -d '{"key":"city"}' | j; echo

echo "== 二进制安全写入（请求体即值字节，键走 query）=="
printf '\x00\x01\x02\xff raw bytes' | \
  curl -s -X POST "$BASE/raw/put?key=blob" --data-binary @- | j; echo

echo "== 统计信息 =="
curl -s "$BASE/stats" | j; echo
