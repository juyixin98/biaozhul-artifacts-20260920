#!/usr/bin/env bash
# 端到端演示脚本：启动服务（若尚未启动），依次调用各接口，展示
# 稳定分页 / 同分按 docId 决胜 / 快照隔离 / 快照过期 410 等关键行为。
#
# 用法：./scripts/demo.sh [端口]   （默认 8080，演示中会在末尾停止服务）
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-8080}"
BASE="http://localhost:$PORT"

echo "############ 0. 启动服务（合成语料，21 篇） ############"
./scripts/build.sh >/dev/null
java -cp out com.bm25stable.Main "$PORT" >/tmp/bm25-demo-server.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

# 等待端口就绪
for _ in $(seq 1 50); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

hr() { echo; echo "############ $1 ############"; }

hr "1. 健康检查"
curl -s "$BASE/health"; echo

hr "2. 首页检索 q=apple pageSize=2（同分时 docId 小的在前）"
P1=$(curl -s "$BASE/search?q=apple&pageSize=2")
echo "$P1" | python3 -m json.tool
C=$(echo "$P1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nextCursor"])')

hr "3. 用 nextCursor 取第 2 页（不漏不重；offset=2）"
P2=$(curl -s "$BASE/search?q=apple&pageSize=2&cursor=$C")
echo "$P2" | python3 -m json.tool
C2=$(echo "$P2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nextCursor"])')

hr "4. 继续取第 3 页（末页 hasMore=false，无 nextCursor）"
curl -s "$BASE/search?q=apple&pageSize=2&cursor=$C2" | python3 -m json.tool

hr "5. 同分决胜：q=cherry，三篇文档内容完全相同，按 docId 升序"
curl -s "$BASE/search?q=cherry&pageSize=10" | python3 -m json.tool

hr "6. 空文档 / 仅标点不会命中：q=apple 时 empty-1、punct-1 不出现"
echo "punctuation-only 查询 '!!! ???' 的结果："
curl -s "$BASE/search?q=!!!%20%3F%3F%3F" | python3 -m json.tool

hr "7. 快照隔离：翻页途中修改语料，旧游标页面不受影响"
echo "-- 新增 new-apple、删除 doc-01 --"
curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' \
  -d '{"id":"new-apple","text":"apple crisp"}'; echo
curl -s -X DELETE "$BASE/documents/doc-01"; echo
echo "-- 重新从头取一个第 1 页游标（此时为 version 3）--"
P1B=$(curl -s "$BASE/search?q=apple&pageSize=2")
CB=$(echo "$P1B" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nextCursor"])')
echo "-- 再制造 2 次变更 --"
curl -s -X POST "$BASE/documents/bulk" -H 'Content-Type: application/json' \
  -d '{"documents":[{"id":"other-1","text":"pear"},{"id":"other-2","text":"plum"}]}' >/dev/null; echo "bulk upsert done"
echo "-- 旧游标取第 2 页：snapshotVersion 仍为 3，totalHits 仍为 5 --"
curl -s "$BASE/search?q=apple&pageSize=2&cursor=$CB" | python3 -m json.tool
echo "-- 不带游标的新搜索看到新文档、看不到已删除文档 --"
curl -s "$BASE/search?q=apple&pageSize=20" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("version:",d["snapshotVersion"],"totalHits:",d["totalHits"]);print([h["docId"] for h in d["hits"]])'

hr "8. 快照过期：制造 8 次变更淘汰旧快照后，旧游标返回 410"
for i in $(seq 0 7); do
  curl -s -X POST "$BASE/documents" -H 'Content-Type: application/json' \
    -d "{\"id\":\"churn-$i\",\"text\":\"churn $i\"}" >/dev/null
done
echo "retained snapshots:"
curl -s "$BASE/snapshots" | python3 -m json.tool
echo "reuse old cursor:"
curl -s -w "\nHTTP %{http_code}\n" "$BASE/search?q=apple&pageSize=2&cursor=$CB"

hr "9. 错误处理（400 / 404 / 405）"
echo -n "missing q:        "; curl -s -w " [%{http_code}]\n" "$BASE/search?pageSize=3"
echo -n "pageSize out range:"; curl -s -w " [%{http_code}]\n" "$BASE/search?q=apple&pageSize=999"
echo -n "garbage cursor:   "; curl -s -w " [%{http_code}]\n" "$BASE/search?q=apple&cursor=garbage%21%21"
echo -n "cursor mismatch:  "; Q=$(curl -s "$BASE/search?q=cherry&pageSize=2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nextCursor"])'); curl -s -w " [%{http_code}]\n" "$BASE/search?q=apple&pageSize=2&cursor=$Q"
echo -n "unknown route:    "; curl -s -w " [%{http_code}]\n" "$BASE/no-such-route"
echo -n "bad json body:    "; curl -s -w " [%{http_code}]\n" -X POST "$BASE/documents" -H 'Content-Type: application/json' -d '{not-json'

echo; echo "演示完成，服务将被自动停止。"
