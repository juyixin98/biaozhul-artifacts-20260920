#!/usr/bin/env bash
# 请求样例：启动服务后（./scripts/run_dev.sh）执行本脚本。
# 依赖 curl 与 jq（jq 仅用于美化，没有 jq 时把 | jq . 删掉即可）。
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== 1) 健康检查 =="
curl -sS "$BASE/healthz"; echo

echo "== 2) 查看漏洞馈送（含签名验证状态）=="
curl -sS "$BASE/api/v1/feed/info" | jq .

echo "== 3) 最小 SBOM 匹配 =="
curl -sS -X POST "$BASE/api/v1/match" \
  -H 'Content-Type: application/json' \
  --data-binary "@$HERE/sbom.minimal.json" | jq .

echo "== 4) 验收 SBOM（依赖环 / 多版本 / 区间边界 / 未知版本）=="
curl -sS -X POST "$BASE/api/v1/match" \
  -H 'Content-Type: application/json' \
  --data-binary "@$HERE/../fixtures/sbom.acceptance.json" | jq .summary

echo "== 5) 内联请求样例：同名不同生态不混淆 =="
curl -sS -X POST "$BASE/api/v1/match" \
  -H 'Content-Type: application/json' \
  -d '{
    "sbom_version": "1.0",
    "packages": [
      {"ecosystem":"npm","name":"json","version":"1.5.0","is_root":true,"dependencies":[]},
      {"ecosystem":"pypi","name":"json","version":"1.5.0","is_root":true,"dependencies":[]}
    ]
  }' | jq '.findings[] | {id: .package.id, vuln: .vulnerability.id, status}'
