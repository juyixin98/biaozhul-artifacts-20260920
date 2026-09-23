#!/usr/bin/env bash
# 端到端演示脚本（验收场景）：需要服务已在 $BASE（默认 http://localhost:8080）启动。
# 逐步：维表插入 -> 订单(含孤儿) -> 视图 -> 维表分类变更 -> 重复事件(同载荷/冲突) ->
#       删除不存在记录 -> 每步与全量重算 diff 比对。
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"
j() { python3 -m json.tool 2>/dev/null || cat; }   # 有 python 就美化，否则原样

echo "### 0. 重置 + 健康检查"
curl -s -X POST "$BASE/admin/reset" | j; echo
curl -s "$BASE/health" | j; echo

echo "### 1. 商品维表：productId=1 -> BOOKS"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d @examples/01-product-upsert.json | j; echo

echo "### 2. 批量订单：101/102 指向商品1；103 指向不存在的商品2（孤儿）"
curl -s -X POST "$BASE/events/batch" -H 'Content-Type: application/json' \
  -d @examples/02-orders-batch.json | j; echo

echo "### 3. 增量视图（预期 BOOKS qty=5 amount=15.50；orphan qty=1 amount=99.00）"
curl -s "$BASE/view" | j; echo

echo "### 4. 维表分类变更：商品 1 BOOKS -> MEDIA"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d @examples/03-category-change.json | j; echo

echo "### 5. 变更后视图（预期 MEDIA qty=5 amount=15.50，BOOKS 消失）"
curl -s "$BASE/view" | j; echo

echo "### 6. 与全量重算比对（预期 consistent=true）"
curl -s "$BASE/view/diff" | j; echo

echo "### 7. 重复业务事件（同 eventId 同载荷，预期 DUPLICATE/conflict=false）"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d @examples/04-duplicate-same-payload.json | j; echo

echo "### 8. 重复业务事件（同 eventId 冲突载荷，预期 DUPLICATE/conflict=true，首达值不变）"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d @examples/05-duplicate-conflict-payload.json | j; echo

echo "### 9. 删除不存在的订单行（预期 APPLIED/ignored=true 空操作）"
curl -s -X POST "$BASE/events" -H 'Content-Type: application/json' \
  -d @examples/06-delete-missing.json | j; echo

echo "### 10. 最终视图 + 再与全量重算比对（视图应与第 5 步完全一致）"
curl -s "$BASE/view" | j; echo
curl -s "$BASE/view/diff" | j; echo
