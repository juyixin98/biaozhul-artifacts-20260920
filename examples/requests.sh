#!/usr/bin/env bash
# 请求样例：先启动服务 bash scripts/server.sh 8080，再运行本脚本。
set -u
BASE="${1:-http://127.0.0.1:8080}"

echo '=== GET /health ==='
curl -s "$BASE/health"; echo

echo '=== GET /documents ==='
curl -s "$BASE/documents"; echo

echo '=== POST /normalize  Straße（大小写展开 ß→ss）==='
curl -s -X POST "$BASE/normalize" -H 'Content-Type: application/json' \
  --data '{"text":"Straße"}'; echo

echo '=== POST /normalize  café 组合形式（NFKD 分解）==='
curl -s -X POST "$BASE/normalize" -H 'Content-Type: application/json' \
  --data '{"text":"café"}'; echo

echo '=== POST /search  STRASSE（命中 Straße）==='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  --data '{"query":"STRASSE"}'; echo

echo '=== POST /search  café 组合形式（命中组合+分解+CAFÉ）==='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  --data '{"query":"café"}'; echo

echo '=== POST /search  ss（展开为完整的 ß）==='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  --data '{"query":"ss"}'; echo

echo '=== POST /search  fine（命中 ﬁne 连字）==='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  --data '{"query":"fine"}'; echo

echo '=== POST /search  emoji（代理对不切断）==='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  --data '{"query":"😀"}'; echo

echo '=== POST /search  日本語（CJK 多字节）==='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  --data '{"query":"日本語"}'; echo

echo '=== POST /documents  新增文档后再检索 ==='
curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' \
  --data '{"id":"doc:new","text":"Grüß Gott, die STRASSE ist schön."}'; echo
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  --data '{"query":"strasse"}'; echo

echo '=== 错误请求（query 非字符串）==='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  --data '{"query":123}'; echo
