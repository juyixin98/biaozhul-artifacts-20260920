#!/usr/bin/env bash
# BM25 稳定分页服务 —— 请求样例集合（curl）。
# 前置：./run.sh 已在本机 8080 端口启动（可用 BM25_PORT 改端口）。
# 输出均为 JSON；可用 | python3 -m json.tool 美化。
set -u
BASE="${BM25_BASE:-http://127.0.0.1:8080}"

echo "### 0. 健康检查"
curl -s "$BASE/health"; echo

echo "### 1. 首页检索（同分 tie 案例，pageSize=2）"
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  -d '{"query":"banana","pageSize":2}'; echo

echo "### 2. 翻到下一页：把上一条响应的 nextCursor 原样回传"
# 注意：游标是不透明字符串，下面用 python 从首页响应中提取，避免手工复制。
CURSOR=$(curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  -d '{"query":"banana","pageSize":2}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["nextCursor"] or "")')
if [ -n "$CURSOR" ]; then
  curl -s -X POST "$BASE/search/continue" -H 'Content-Type: application/json' \
    -d "{\"cursor\":\"$CURSOR\"}"; echo
fi

echo "### 3. 大结果集连续分页（查询 data，pageSize=7，共 60 条）"
echo "# 逐页跟随 nextCursor 直到 hasMore=false；每页 version 都相同。"

echo "### 4. 空查询（合法，返回空结果集）"
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' -d '{"query":""}'; echo

echo "### 5. 查询词重复（data DATA Data 与 data 等价，词项在打分前去重）"
curl -s -X POST "$BASE/search" -H 'Content-Type: application/json' \
  -d '{"query":"data DATA Data","pageSize":5}'; echo

echo "### 6. 新增/更新文档（自动发布新版本快照）"
curl -s -X POST "$BASE/documents/upsert" -H 'Content-Type: application/json' \
  -d '{"docId":"live-new","content":"data data data ranking","metadata":{"title":"live insert"}}'; echo

echo "### 7. 删除文档"
curl -s -X DELETE "$BASE/documents/live-new"; echo

echo "### 8. 显式发布快照（批量修改后一次性生效）"
curl -s -X POST "$BASE/admin/commit" -H 'Content-Type: application/json' -d '{}'; echo

echo "### 9. 驱逐历史快照（keep=1），随后旧游标翻页将得到 410 SNAPSHOT_EXPIRED"
curl -s -X POST "$BASE/admin/compact?keep=1" -H 'Content-Type: application/json' -d '{}'; echo

echo "### 10. 索引状态"
curl -s "$BASE/admin/status"; echo

echo "### 11. 错误样例：损坏游标 -> 400 INVALID_CURSOR"
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE/search/continue" \
  -H 'Content-Type: application/json' -d '{"cursor":"%%%garbage"}'
