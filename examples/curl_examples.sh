#!/usr/bin/env bash
# 通过本地 HTTP API 使用本服务的示例（需要先启动服务）：
#   PYTHONPATH=src python3 -m mde.cli serve --store-dir ./demo/policies --port 8080
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8080}
HERE=$(cd "$(dirname "$0")" && pwd)

echo "== 1. 健康检查 =="
curl -sS "$BASE/health" | python3 -m json.tool

echo "== 2. 发布策略 v1 =="
curl -sS -X POST "$BASE/policies/hr-export/publish" \
  -H 'Content-Type: application/json' \
  --data-binary @"$HERE/policy_hr.json" | python3 -m json.tool

echo "== 3. 明文导出（固定 v1，用途 analytics）=="
curl -sS -X POST "$BASE/v1/exports" \
  -H 'Content-Type: application/json' \
  --data-binary @"$HERE/request_export.json" | python3 -m json.tool

echo "== 4. 加密导出（服务端生成一次性 Fernet 密钥，仅返回一次）=="
curl -sS -X POST "$BASE/v1/exports" \
  -H 'Content-Type: application/json' \
  --data-binary @"$HERE/request_export_encrypted.json" | python3 -m json.tool

echo "== 5. 基于过期版本号发布（乐观锁竞争，预期 409）=="
curl -sS -X POST "$BASE/policies/hr-export/publish?expected_version=1" \
  -H 'Content-Type: application/json' \
  --data-binary @"$HERE/request_policy_publish.json" | python3 -m json.tool || true

echo
echo "核验请用 POST $BASE/v1/exports/verify，body 形如："
echo '{"bundle": <上一步返回的 bundle>, "source_data": <原始数据对象>}'
