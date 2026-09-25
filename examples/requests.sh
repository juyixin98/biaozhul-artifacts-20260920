#!/usr/bin/env bash
# 透明日志 HTTP API 请求样例（curl）。
# 用法：
#   终端 1：python3 -m tlog.cli serve --port 8080 --data-dir ./log_data
#   终端 2：bash examples/requests.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"

echo "### 0. 健康检查"
curl -s "$BASE/health" | python3 -m json.tool

echo "### 1. 追加叶子（UTF-8 文本）"
curl -s -X POST "$BASE/add" \
  -H 'Content-Type: application/json' \
  -d '{"data_utf8": "证书记录 alpha"}' | python3 -m json.tool

curl -s -X POST "$BASE/add" \
  -H 'Content-Type: application/json' \
  -d '{"data_utf8": "证书记录 beta"}' | python3 -m json.tool

echo "### 2. 追加二进制叶子（base64：0x00 0x01 0x02）"
curl -s -X POST "$BASE/add" \
  -H 'Content-Type: application/json' \
  -d '{"data_b64": "AAEC"}' | python3 -m json.tool

echo "### 3. 获取签名树头 STH（含 Ed25519 签名）"
curl -s "$BASE/sth" | python3 -m json.tool

echo "### 4. 获取叶子 0 的包含证明"
curl -s "$BASE/get-inclusion-proof?leaf_index=0" | python3 -m json.tool

echo "### 5. 获取叶子 2 在历史树大小 3 上的包含证明"
curl -s "$BASE/get-inclusion-proof?leaf_index=2&tree_size=3" | python3 -m json.tool

echo "### 6. 从旧大小 1 到当前大小的一致性证明"
curl -s "$BASE/get-consistency-proof?first=1" | python3 -m json.tool

echo "### 7. 读取叶子内容"
curl -s "$BASE/get-leaf?index=0" | python3 -m json.tool

echo
echo "### 客户端本地验证（不使用服务端的任何结论，只信已验签的 STH）："
echo "python3 -m tlog.cli inclusion 0"
echo "python3 -m tlog.cli consistency 1"
