#!/usr/bin/env bash
# 请求样例：先启动服务
#   java -jar target/edit-distance-filter-1.0.0.jar --port 18099 --corpus-size 2000 --seed 42
set -euo pipefail
BASE="${BASE:-http://localhost:18099}"

echo '== GET /health =='
curl -s "$BASE/health"; echo

echo '== GET /corpus =='
curl -s "$BASE/corpus"; echo

echo '== POST /search 精确+模糊（apple, k=2）=='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  -d '{"query":"apple","k":2,"limit":10}'; echo

echo '== POST /search 组合字符：NFD 查询命中 NFC 词条（k=0）=='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  -d '{"query":"cafe\u0301","k":0}'; echo

echo '== POST /search emoji 按单码点计（k=1）=='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  -d '{"query":"😀","k":1}'; echo

echo '== POST /search 空串查询（k=0 只命中空串）=='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  -d '{"query":"","k":0}'; echo

echo '== POST /search CJK（k=1）=='
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  -d '{"query":"编辑","k":1}'; echo

echo '== POST /corpus/reload 重建语料 =='
curl -s -X POST "$BASE/corpus/reload" -H 'Content-Type: application/json' \
  -d '{"size":500,"seed":7}'; echo

echo '== 错误样例：缺 query（400）=='
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/search" \
  -H 'Content-Type: application/json' -d '{"k":2}'

echo '== 错误样例：k 越界（400）=='
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/search" \
  -H 'Content-Type: application/json' -d '{"query":"a","k":99}'

echo '== 错误样例：GET /search（405）=='
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/search"
