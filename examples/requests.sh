#!/usr/bin/env bash
# keyvault 请求样例：完整走一遍 生成→激活→加密→轮换→解密→停用→销毁→验证拒绝
# 前提：服务已在运行  python3 -m keyvault.server --data-dir ./demo-data --port 8765
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8765}"
JQ=""

if command -v jq >/dev/null 2>&1; then JQ="jq"; else JQ="python3 -m json.tool"; fi

step() { echo; echo "=== $* ==="; }

step "1. 生成密钥版本 v1"
curl -s -X POST "$BASE/keys" | $JQ

step "2. 激活 v1"
curl -s -X POST "$BASE/keys/v1/activate" | $JQ

step "3. 用激活版本加密（明文 base64: aGVsbG8ga2V5dmF1bHQ= 即 'hello keyvault'）"
ENVELOPE=$(curl -s -X POST "$BASE/encrypt" \
  -H 'Content-Type: application/json' \
  -d '{"plaintext_b64": "aGVsbG8ga2V5dmF1bHQ="}' | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin)["envelope"]))')
echo "$ENVELOPE" | $JQ

step "4. 解密刚才的密文"
curl -s -X POST "$BASE/decrypt" \
  -H 'Content-Type: application/json' \
  -d "{\"envelope\": $ENVELOPE}" | $JQ

step "5. 轮换：生成 v2 并激活（v1 自动停用）"
curl -s -X POST "$BASE/keys" | $JQ
curl -s -X POST "$BASE/keys/v2/activate" | $JQ

step "6. 历史密文仍可用 v1 解密（历史版本权限）"
curl -s -X POST "$BASE/decrypt" \
  -H 'Content-Type: application/json' \
  -d "{\"envelope\": $ENVELOPE}" | $JQ

step "7. 错误版本拒绝：密文是 v1 加密的，却声明 expect_version=v2 -> 422"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/decrypt" \
  -H 'Content-Type: application/json' \
  -d "{\"envelope\": $ENVELOPE, \"expect_version\": \"v2\"}"
curl -s -X POST "$BASE/decrypt" \
  -H 'Content-Type: application/json' \
  -d "{\"envelope\": $ENVELOPE, \"expect_version\": \"v2\"}" | $JQ

step "8. 销毁 v1（先停用确认：v1 已被轮换自动停用，直接销毁）"
curl -s -X POST "$BASE/keys/v1/destroy" | $JQ

step "9. 销毁后密文明确不可恢复 -> 410"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/decrypt" \
  -H 'Content-Type: application/json' \
  -d "{\"envelope\": $ENVELOPE}"
curl -s -X POST "$BASE/decrypt" \
  -H 'Content-Type: application/json' \
  -d "{\"envelope\": $ENVELOPE}" | $JQ

step "10. 非法迁移：销毁已销毁的版本 -> 409"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/keys/v1/destroy"

step "11. 服务状态与一致性自检"
curl -s "$BASE/status" | $JQ

step "12. 审计日志哈希链校验"
curl -s "$BASE/audit/verify" | $JQ

step "13. 审计日志（注意：只有摘要，无密钥、无明文）"
curl -s "$BASE/audit" | $JQ
