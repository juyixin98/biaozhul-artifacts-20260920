#!/usr/bin/env bash
# 对本地可复现归档服务的一组请求样例。先启动服务：
#   go run ./cmd/server -addr 127.0.0.1:8080
set -euo pipefail

BASE="${BASE_URL:-http://127.0.0.1:8080}"
SRC="${1:?用法: $0 <待打包源目录绝对路径> [制品输出绝对路径]}"
OUT="${2:-/tmp/reproducible-artifact.tar}"

echo '== 1) 健康探针 =='
curl -sS "$BASE/healthz"; echo

echo '== 2) 发起确定性构建（默认 0644、epoch mtime）=='
RESP="$(curl -sS -X POST "$BASE/v1/builds" \
  -H 'Content-Type: application/json' \
  -d "{\"source_dir\":\"$SRC\",\"output_path\":\"$OUT\"}")"
echo "$RESP" | jq .
BUILD_ID="$(echo "$RESP" | jq -r .build_id)"

echo '== 3) 再构建一次（同样内容 -> cache_hit 应为 true，哈希一致）=='
curl -sS -X POST "$BASE/v1/builds" \
  -H 'Content-Type: application/json' \
  -d "{\"source_dir\":\"$SRC\",\"output_path\":\"${OUT}.again\"}" | jq .

echo '== 4) 查询单次构建 =='
curl -sS "$BASE/v1/builds/$BUILD_ID" | jq .

echo '== 5) 列出全部构建 =='
curl -sS "$BASE/v1/builds" | jq '.builds[] | {build_id, artifact_sha256, cache_hit, status}'

echo '== 6) 预期失败：逃逸符号链接（422）=='
TMP="$(mktemp -d)"
ln -s '../../../../etc/passwd' "$TMP/evil"
curl -sS -i -X POST "$BASE/v1/builds" \
  -H 'Content-Type: application/json' \
  -d "{\"source_dir\":\"$TMP\",\"output_path\":\"/tmp/should-not-exist.tar\"}" | sed -n '1,12p'
rm -rf "$TMP"
