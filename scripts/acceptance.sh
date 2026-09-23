#!/usr/bin/env bash
# 验收脚本：构造“跨三层同键覆盖后删除”，验证两种合并中断 + 重启后
# 旧值不复活、范围扫描无重复；最后正式合并并再次重启校验。
#
# 用法: ./scripts/acceptance.sh [数据目录] [端口]
set -u

DIR="${1:-/tmp/lsm-accept}"
PORT="${2:-31234}"
BASE="http://127.0.0.1:${PORT}"

FAIL=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=1; }

# 极简 JSON 取值（依赖系统 python3 做断言；无 python3 时退化为字符串检查）
assert_eq() { # desc actual expected
  if [ "$2" == "$3" ]; then pass "$1"; else fail "$1 (got: $2, want: $3)"; fi
}
json_get() { python3 -c 'import json,sys; print(json.load(sys.stdin)'"$1"')' 2>/dev/null; }

rm -rf "$DIR"
mkdir -p "$DIR"

cargo run --quiet -- --dir "$DIR" --port "$PORT" --memtable-entries 2 >/tmp/lsm-accept.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  curl -sf "$BASE/state" >/dev/null 2>&1 && break
  sleep 0.1
done

req() { curl -s -X "$1" "$BASE$2" ${3:+-H 'content-type: application/json' -d "$3"}; }

echo "== 构造三层：seg1 k=v1 / seg2 k=v2 / seg3 k=tombstone =="
req POST /put '{"key":"k","value":"v1"}'; req POST /put '{"key":"a","value":"1"}'
req POST /flush >/dev/null
req POST /put '{"key":"k","value":"v2"}'; req POST /put '{"key":"b","value":"2"}'
req POST /flush >/dev/null
req POST /delete '{"key":"k"}'; req POST /put '{"key":"c","value":"3"}'
req POST /flush >/dev/null

NSEG=$(req GET /state | json_get "['segments'].__len__()")
assert_eq "三层段已形成" "$NSEG" "3"

FOUND=$(req GET '/get?key=k' | json_get "['found']")
assert_eq "点查 k 被墓碑遮蔽" "$FOUND" "False"

SCAN=$(req GET /scan | json_get "['items']")
assert_eq "扫描无重复且无 k" "$SCAN" "[['a', '1'], ['b', '2'], ['c', '3']]"

echo "== 故障点1：新段落盘、MANIFEST 未切换 -> 重启 =="
CRASH=$(req POST /compact '{"mode":"full","crash":"after_new_segment_before_manifest"}')
echo "$CRASH" | grep -q crash && pass "接口返回崩溃注入错误" || fail "未触发崩溃注入"
kill $SRV; wait $SRV 2>/dev/null
[ -f "$DIR/segments/4.seg" ] && pass "新段已落盘(孤儿)" || fail "新段文件不存在"

cargo run --quiet -- --dir "$DIR" --port "$PORT" --memtable-entries 2 >>/tmp/lsm-accept.log 2>&1 &
SRV=$!
for _ in $(seq 1 50); do curl -sf "$BASE/state" >/dev/null 2>&1 && break; sleep 0.1; done

[ ! -f "$DIR/segments/4.seg" ] && pass "重启后孤儿段被清理" || fail "孤儿段未清理"
NSEG=$(req GET /state | json_get "['segments'].__len__()")
assert_eq "清单回到崩溃前的3段" "$NSEG" "3"
assert_eq "重启后点查旧值不复活" "$(req GET '/get?key=k' | json_get "['found']")" "False"
assert_eq "重启后扫描无重复" "$(req GET /scan | json_get "['items']")" \
  "[['a', '1'], ['b', '2'], ['c', '3']]"

echo "== 故障点2：MANIFEST 已切换、旧段未删 -> 重启 =="
req POST /compact '{"mode":"full","crash":"after_manifest_before_delete_old"}' >/dev/null
kill $SRV; wait $SRV 2>/dev/null
# 注意：故障点2在进程内已更新清单，4.seg 被新清单引用；旧 1/2/3.seg 残留
for id in 1 2 3; do
  [ -f "$DIR/segments/$id.seg" ] && pass "旧段 $id.seg 残留(等待重启回收)" || fail "旧段 $id 未找到"
done

cargo run --quiet -- --dir "$DIR" --port "$PORT" --memtable-entries 2 >>/tmp/lsm-accept.log 2>&1 &
SRV=$!
for _ in $(seq 1 50); do curl -sf "$BASE/state" >/dev/null 2>&1 && break; sleep 0.1; done

for id in 1 2 3; do
  [ ! -f "$DIR/segments/$id.seg" ] && pass "重启后旧段 $id 被回收" || fail "旧段 $id 未回收"
done
NSEG=$(req GET /state | json_get "['segments'].__len__()")
assert_eq "合并后仅1段" "$NSEG" "1"
assert_eq "点查旧值不复活" "$(req GET '/get?key=k' | json_get "['found']")" "False"
assert_eq "扫描无重复" "$(req GET /scan | json_get "['items']")" \
  "[['a', '1'], ['b', '2'], ['c', '3']]"

echo "== 再次重启（墓碑已合法丢弃，确认持久正确） =="
kill $SRV; wait $SRV 2>/dev/null
cargo run --quiet -- --dir "$DIR" --port "$PORT" --memtable-entries 2 >>/tmp/lsm-accept.log 2>&1 &
SRV=$!
for _ in $(seq 1 50); do curl -sf "$BASE/state" >/dev/null 2>&1 && break; sleep 0.1; done
assert_eq "二次重启后旧值仍不复活" "$(req GET '/get?key=k' | json_get "['found']")" "False"
assert_eq "二次重启后扫描无重复" "$(req GET /scan | json_get "['items']")" \
  "[['a', '1'], ['b', '2'], ['c', '3']]"

kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
trap - EXIT

if [ "$FAIL" -eq 0 ]; then
  echo
  echo "ALL ACCEPTANCE CHECKS PASSED"
else
  echo
  echo "ACCEPTANCE FAILED (server log: /tmp/lsm-accept.log)"
  exit 1
fi
