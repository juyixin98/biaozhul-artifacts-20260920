#!/usr/bin/env bash
# 端到端演示：启动服务 -> 稳定翻页 -> 翻页途中增删改 -> 篡改游标 -> 快照过期 -> 关闭
# 用法：scripts/demo.sh [输出文件]
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-/tmp/demo-output.txt}"
PORT=18099
TTL=3   # 3 秒，便于演示过期
BASE="http://localhost:$PORT"

if [ -n "${JAVA_HOME:-}" ]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="$(command -v java)"
fi

: > "$OUT"
say() { echo -e "$@" | tee -a "$OUT"; }
req() {
  # req <说明> <curl参数...>
  local note="$1"; shift
  say "\n\$ curl $*"
  say "# $note"
  curl -sS -w '\n# HTTP %{http_code}\n' "$@" | tee -a "$OUT"
}

if [ ! -d build/classes ]; then
  scripts/build.sh >/dev/null
fi

PORT="$PORT" SNAPSHOT_TTL="$TTL" CURSOR_SECRET="demo-secret-0123456789" \
  "$JAVA" -cp build/classes com.example.paginate.Main >/tmp/demo-server.log 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

# 等待端口就绪
for _ in $(seq 1 50); do
  if curl -sS "$BASE/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

say "========== 0. 健康检查 =========="
req "服务状态与 TTL" "$BASE/healthz"

say "\n========== 1. 第一页（建立快照 v1，pageSize=5，按 name_asc）=========="
FIRST=$(curl -sS "$BASE/api/items?sort=name_asc&pageSize=5")
echo "$FIRST" | tee -a "$OUT"
CURSOR=$(echo "$FIRST" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nextCursor"] or "")')
SNAP=$(echo "$FIRST" | python3 -c 'import sys,json;print(json.load(sys.stdin)["snapshotId"])')
say "\n# 保存 snapshotId=$SNAP 与 nextCursor（后续页复用同一快照）"

say "\n========== 2. 翻页途中制造变更（插入/删除/改排序键）=========="
req "插入一条 zzz-injected（若进入结果集会排在 name 序末尾）" \
  -H 'Content-Type: application/json' \
  -d '{"name":"zzz-injected","category":"books","score":999}' \
  -X POST "$BASE/api/admin/items"

req "删除 id=10（alpha 的重复项之一，属于尚未翻到的页）" \
  -X DELETE "$BASE/api/admin/items/10"

req "把 id=50 的排序键 name 改为 aaa-moved（新查询里会排到最前）" \
  -H 'Content-Type: application/json' \
  -d '{"name":"aaa-moved","score":5}' \
  -X PATCH "$BASE/api/admin/items/50"

say "\n========== 3. 继续用旧游标翻页（仍是快照 v1 的内容，不重不漏）=========="
ALL_IDS='[]'
PAGE=2
while [ -n "$CURSOR" ]; do
  say "\n--- 第 $PAGE 页（快照 v1）---"
  RESP=$(curl -sS "$BASE/api/items?sort=name_asc&pageSize=5&cursor=$CURSOR")
  echo "$RESP" | python3 -m json.tool | tee -a "$OUT"
  CURSOR=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["nextCursor"] or "")')
  PAGE=$((PAGE+1))
done

say "\n========== 4. 重新发起第一页（新快照 v2，反映全部变更）=========="
req "新快照第一条应为 aaa-moved(id=50)，zzz-injected 在末尾，id=10 消失" \
  "$BASE/api/items?sort=name_asc&pageSize=100"

say "\n========== 5. 变更筛选/排序后复用旧游标 -> 400 cursor_query_mismatch =========="
req "第一页：category=books" "$BASE/api/items?sort=name_asc&category=books&pageSize=5"
OLD=$(curl -sS "$BASE/api/items?sort=name_asc&category=books&pageSize=5" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["nextCursor"])')
req "同一游标改成 category=movies -> 拒绝" \
  "$BASE/api/items?sort=name_asc&category=movies&pageSize=5&cursor=$OLD"
req "同一游标改成 sort=score_desc -> 拒绝" \
  "$BASE/api/items?sort=score_desc&category=books&pageSize=5&cursor=$OLD"

say "\n========== 6. 伪造/篡改游标 -> 403 cursor_invalid =========="
GOOD=$(curl -sS "$BASE/api/items?sort=name_asc&pageSize=5" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["nextCursor"])')
BAD="${GOOD/X/Y}"
[ "$BAD" = "$GOOD" ] && BAD="${GOOD/A/B}"
req "翻转游标一个字符 -> MAC 校验失败" \
  "$BASE/api/items?sort=name_asc&pageSize=5&cursor=$BAD"
req "完全编造的游标 -> MAC 校验失败" \
  "$BASE/api/items?sort=name_asc&pageSize=5&cursor=forged.cursor"

say "\n========== 7. 快照过期明确报错（TTL=${TTL}s）=========="
EXPIRE_CURSOR=$(curl -sS "$BASE/api/items?sort=name_asc&pageSize=5" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["nextCursor"])')
say "# 拿到游标后等待 $((TTL+1)) 秒……"
sleep $((TTL+1))
req "旧快照已过期 -> 410 snapshot_expired" \
  "$BASE/api/items?sort=name_asc&pageSize=5&cursor=$EXPIRE_CURSOR"

say "\n========== 8. 当前真实数据统计 =========="
req "当前总数" "$BASE/api/admin/stats"

say "\n演示完成，完整输出见 $OUT"
kill $SERVER_PID 2>/dev/null || true
trap - EXIT
