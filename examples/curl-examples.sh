#!/usr/bin/env bash
# 短语位置检索服务 — curl 请求样例（复制即可运行）。
# 前提：scripts/run_server.sh --port 8080
set -u

BASE="${BASE:-http://127.0.0.1:8080}"

echo "### [1] health"; curl -s "$BASE/health"; echo
echo "### [2] config（停用词/slop/跨字段语义）"; curl -s "$BASE/config"; echo
echo "### [3] docs（全局位置流）"; curl -s "$BASE/docs" | head -40; echo

echo "### [4] 精确短语 quick brown fox, slop=0"
curl -s "$BASE/search?q=quick%20brown%20fox&slop=0"; echo

echo "### [5] 重复词 that that, slop=0"
curl -s "$BASE/search?q=that%20that&slop=0"; echo

echo "### [6] 重复词 that that, slop=5（8 对，位置互不相同）"
curl -s "$BASE/search?q=that%20that&slop=5"; echo

echo "### [7] 三连重复 had had, slop=0"
curl -s "$BASE/search?q=had%20had&slop=0"; echo

echo "### [8a] 跨字段 phrase search, slop=0"
curl -s "$BASE/search?q=phrase%20search&slop=0"; echo
echo "### [8b] 跨字段 phrase search, slop=2（多出 title->body 边界命中）"
curl -s "$BASE/search?q=phrase%20search&slop=2"; echo

echo "### [9] 跨字段 + 停用词 system the quick, slop=0"
curl -s "$BASE/search?q=system%20the%20quick&slop=0"; echo

echo "### [10] 字段限定 body: phrase search 不命中"
curl -s "$BASE/search?q=phrase%20search&slop=2&field=body"; echo

echo "### [11] POST JSON"
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  -d '{"query":"alpha alpha","slop":4}'; echo

echo "### [12] analyze"
curl -s -X POST "$BASE/analyze" -H 'Content-Type: application/json' \
  -d '{"text":"The quick, brown fox!"}'; echo

echo "### [13] 错误：缺少 query -> 400"
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' -d '{"slop":0}'; echo
