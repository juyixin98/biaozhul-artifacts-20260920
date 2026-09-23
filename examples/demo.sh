#!/usr/bin/env bash
# 端到端演示：长读事务跨越三次覆盖+删除、写写冲突、快照关闭前后 GC。
# 用法：先启动服务（cargo run --release），再运行本脚本：bash examples/demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://127.0.0.1:8080}"

# URL-safe base64（- 与 _），可直接放入路径段。
# 字符顺序经调整使 '-' 位于 tr 字符集末尾，避免被解析为区间。
b64() { printf '%s' "$1" | base64 -w0 | tr '/+' '_-'; }
b64d() { printf '%s' "$1" | tr '_-' '/+' | base64 -d; }

# req METHOD URI [JSON_BODY] -> 原样输出响应体
req() {
  local method="$1" uri="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" -H 'content-type: application/json' -d "$body" "$BASE$uri"
  else
    curl -sS -X "$method" "$BASE$uri"
  fi
}

# json_field JSON .field -> 字段值（null 输出空串）。jq 优先，否则 python3。
json_field() {
  if command -v jq >/dev/null; then
    jq -r "$2 // empty" <<<"$1"
  elif command -v python3 >/dev/null; then
    local pyexpr
    pyexpr=$(sed 's/^\.\([A-Za-z_][A-Za-z0-9_]*\)$/["\1"]/' <<<"$2")
    python3 -c "import json,sys; v=json.load(sys.stdin); r=v$pyexpr; print('' if r is None else r)" <<<"$1"
  else die "need jq or python3"; fi
}
die() { echo "ERROR: $*" >&2; exit 1; }

echo "== 健康检查 =="
req GET /health; echo

echo "== 初始写入 k1=v1 并提交 =="
resp=$(req POST /txn); echo "$resp"
t0=$(json_field "$resp" ".txn_id")
req POST "/txn/$t0/put/$(b64 k1)" "{\"value\":\"$(b64 v1)\"}" >/dev/null
req POST "/txn/$t0/commit"; echo

echo "== 开启长读事务 reader（快照点只能看到 v1）=="
resp=$(req POST /txn); echo "$resp"
reader=$(json_field "$resp" ".txn_id")
reader_ts=$(json_field "$resp" ".start_ts")
r=$(req GET "/txn/$reader/get/$(b64 k1)")
echo "reader 读 k1: $r  ($(b64d "$(json_field "$r" ".value")"))"

echo "== 三个事务依次覆盖 k1=v2/v3/v4 =="
for v in v2 v3 v4; do
  w=$(json_field "$(req POST /txn)" ".txn_id")
  req POST "/txn/$w/put/$(b64 k1)" "{\"value\":\"$(b64 "$v")\"}" >/dev/null
  echo "提交 $v -> $(req POST "/txn/$w/commit")"
done

echo "== 事务删除 k1（墓碑）并提交 =="
d=$(json_field "$(req POST /txn)" ".txn_id")
req POST "/txn/$d/delete/$(b64 k1)" >/dev/null
req POST "/txn/$d/commit"; echo

echo "== 另一键 k2：写入后再删除（最终仅余墓碑，用于展示整条移除）=="
p=$(json_field "$(req POST /txn)" ".txn_id")
req POST "/txn/$p/put/$(b64 k2)" "{\"value\":\"$(b64 z1)\"}" >/dev/null
req POST "/txn/$p/commit" >/dev/null
d2=$(json_field "$(req POST /txn)" ".txn_id")
req POST "/txn/$d2/delete/$(b64 k2)" >/dev/null
req POST "/txn/$d2/commit" >/dev/null

echo "== 旧读不变：reader 仍见 v1；新事务读到 null（已删除）=="
r=$(req GET "/txn/$reader/get/$(b64 k1)")
echo "reader 读 k1: $r  ($(b64d "$(json_field "$r" ".value")"))"
fresh=$(json_field "$(req POST /txn)" ".txn_id")
echo "新事务读 k1: $(req GET "/txn/$fresh/get/$(b64 k1)")"
req POST "/txn/$fresh/rollback" >/dev/null   # 立即关闭，避免钉住水位线

echo "== 版本链（5 个版本：v1 v2 v3 v4 墓碑）=="
req GET "/admin/versions/$(b64 k1)"; echo

echo "== 并发写 c1/c2 同一键，仅一个可提交 =="
c1=$(json_field "$(req POST /txn)" ".txn_id")
c2=$(json_field "$(req POST /txn)" ".txn_id")
req POST "/txn/$c1/put/$(b64 k1)" "{\"value\":\"$(b64 w1)\"}" >/dev/null
req POST "/txn/$c2/put/$(b64 k1)" "{\"value\":\"$(b64 w2)\"}" >/dev/null
echo "c1 commit: $(req POST "/txn/$c1/commit")"
echo "c2 commit（期望 409 冲突）:"
curl -sS -o /tmp/c2resp -w "HTTP %{http_code} body=" -X POST "$BASE/txn/$c2/commit"; cat /tmp/c2resp; echo

echo "== 快照开着时 GC：水位线=reader 的 ts，回收 0 个版本 =="
req POST /admin/gc; echo

echo "== 记录回收前当前视图（读后即关事务），然后关闭 reader =="
before_txn=$(json_field "$(req POST /txn)" ".txn_id")
before=$(json_field "$(req GET "/txn/$before_txn/get/$(b64 k1)")" ".value")
req POST "/txn/$before_txn/rollback" >/dev/null
echo "回收前当前 k1 = $before ($(b64d "$before"))"
req POST "/txn/$reader/rollback" >/dev/null
req GET /admin/watermark; echo

echo "== 无存活快照，执行 GC：旧版本与墓碑回收，空键整条移除 =="
req POST /admin/gc; echo

echo "== 回收后当前视图与回收前一致；版本链只剩 1 个版本 =="
after_txn=$(json_field "$(req POST /txn)" ".txn_id")
after=$(json_field "$(req GET "/txn/$after_txn/get/$(b64 k1)")" ".value")
echo "回收后当前 k1 = $after ($(b64d "$after"))"
[[ "$before" == "$after" ]] && echo "OK: 回收前后可见结果一致" || die "可见结果不一致!"
req GET "/admin/versions/$(b64 k1)"; echo

# 清理
req POST "/txn/$after_txn/rollback" >/dev/null
echo "演示完成。"
