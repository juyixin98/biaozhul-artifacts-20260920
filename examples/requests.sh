#!/usr/bin/env bash
# HTTP 接口请求样例。用法：
#   ./target/release/bptree-server --db data/demo.db --page-size 64 --no-sync &
#   bash examples/requests.sh
set -u
B="${BPTREE_BASE:-http://127.0.0.1:3000}"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "### 1. 健康检查"
curl -s "$B/healthz"; echo; echo

echo "### 2. 插入 / 覆盖 (PUT /keys/{key})，body: {\"value\": i64}"
for k in 1 2 3 4 5 6 7 8 9 10; do
  curl -s -X PUT "$B/keys/$k" -H 'content-type: application/json' \
    -d "{\"value\": $((k * 10))}"
  echo
done
echo "-- 覆盖已存在的键（200 + replaced）--"
curl -s -X PUT "$B/keys/5" -H 'content-type: application/json' -d '{"value": 555}'; echo; echo

echo "### 3. 点查 (GET /keys/{key})"
curl -s "$B/keys/5"; echo
curl -s "$B/keys/999"; echo; echo

echo "### 4. 有序范围读 (GET /range?start=&end=&limit=)，闭区间，边界与 limit 均可省略"
curl -s "$B/range?start=3&end=7" | j; echo
curl -s "$B/range?limit=3"; echo; echo

echo "### 5. 删除 (DELETE /keys/{key})"
curl -s -X DELETE "$B/keys/5"; echo
curl -s -X DELETE "$B/keys/5"; echo; echo

echo "### 6. 统计 (GET /stats)：页大小、容量、根、空闲链表等"
curl -s "$B/stats" | j; echo

echo "### 7. 全量结构校验 (POST /verify)：分隔键/占用率/叶链"
curl -s -X POST "$B/verify" | j; echo

echo "### 8. 错误样例"
curl -s "$B/keys/abc"; echo
