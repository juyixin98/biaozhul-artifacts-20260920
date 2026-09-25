#!/usr/bin/env bash
# 请求样例：演示 ETag / If-Match 条件更新的完整流程。
# 用法: bash samples/requests.sh [base-url]   （默认 http://127.0.0.1:8080）
set -u
BASE="${1:-http://127.0.0.1:8080}"
R="demo-$RANDOM"

echo "== 1. 缺少前置条件 -> 428 =="
curl -s -i -X PUT "$BASE/resources/$R" -d 'no precondition' | head -1

echo "== 2. If-None-Match: * 创建 -> 200，取得 ETag =="
ETAG=$(curl -s -i -X PUT "$BASE/resources/$R" -H 'If-None-Match: *' -d 'version-1 content' | grep -i '^etag:' | tr -d '\r' | awk '{print $2}')
echo "ETag=$ETAG"

echo "== 3. 重复创建 -> 412 =="
curl -s -o /dev/null -w '%{http_code}\n' -X PUT "$BASE/resources/$R" -H 'If-None-Match: *' -d 'again'

echo "== 4. 正确 If-Match 更新 -> 200，ETag 变化 =="
curl -s -i -X PUT "$BASE/resources/$R" -H "If-Match: $ETAG" -d 'version-2 content' | grep -iE '^(HTTP|etag:)'

echo "== 5. 旧 ETag 再更新（模拟竞争失败方）-> 412 =="
curl -s -o /dev/null -w '%{http_code}\n' -X PUT "$BASE/resources/$R" -H "If-Match: $ETAG" -d 'stale write'

echo "== 6. 弱 ETag 不能用于强比较 -> 412 =="
ETAG2=$(curl -s -D - -o /dev/null "$BASE/resources/$R" | grep -i '^etag:' | tr -d '\r' | awk '{print $2}')
curl -s -o /dev/null -w '%{http_code}\n' -X PUT "$BASE/resources/$R" -H "If-Match: W/$ETAG2" -d 'weak write'

echo "== 7. If-Match: * 通配更新 -> 200 =="
curl -s -o /dev/null -w '%{http_code}\n' -X PUT "$BASE/resources/$R" -H 'If-Match: *' -d 'wildcard write'

echo "== 8. 注入下游故障后更新 -> 503，且无副作用 =="
curl -s -X POST "$BASE/admin/faults" -d '{"failNext":1}' > /dev/null
ETAG3=$(curl -s -D - -o /dev/null "$BASE/resources/$R" | grep -i '^etag:' | tr -d '\r' | awk '{print $2}')
curl -s -o /dev/null -w '%{http_code}\n' -X PUT "$BASE/resources/$R" -H "If-Match: $ETAG3" -d 'must not land'
echo "失败后 ETag 应保持 $ETAG3 :"
curl -s -D - -o /dev/null "$BASE/resources/$R" | grep -i '^etag:' | tr -d '\r'

echo "== 9. 删除（带 If-Match）-> 204，重建后旧 ETag 失效 =="
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE "$BASE/resources/$R" -H "If-Match: $ETAG3"
curl -s -o /dev/null -w '%{http_code}\n' -X PUT "$BASE/resources/$R" -H 'If-None-Match: *' -d 'recreated'
curl -s -o /dev/null -w '%{http_code}\n' -X PUT "$BASE/resources/$R" -H "If-Match: $ETAG3" -d 'stale after recreate'

echo "== 10. 可控时钟 =="
curl -s -X POST "$BASE/admin/clock" -d '{"advanceMs":60000}'; echo
