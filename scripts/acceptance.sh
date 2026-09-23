#!/usr/bin/env bash
# 端到端验收脚本（自包含，无需预先启动服务）：
#   1. 启动 lsm-server；构造跨三层（L0/L1/L2）同键覆盖 v1/v2/v3 后删除；
#   2. 合并 L0+L1 时注入 exit(42) 崩溃（新段已 fsync、manifest 未切换）；
#   3. 用同一数据目录重启：旧值不复活、范围扫描无重复无墓碑键、清单未被改写；
#   4. 恢复后继续完成两级合并：墓碑先保留后丢弃，旧值仍不复活；再次重启验证。
# 用法：bash scripts/acceptance.sh [target/release/lsm-server | cargo 运行]
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-3100}"
BASE="http://127.0.0.1:${PORT}"
DATA="$(mktemp -d)/lsm-acceptance"
LOG1="$(mktemp)"; LOG2="$(mktemp)"; LOG3="$(mktemp)"
BIN="${1:-$HERE/target/release/lsm-server}"
mkdir -p "$DATA"

C=0
step() { C=$((C+1)); printf '\n=== [%d] %s ===\n' "$C" "$*"; }
fail() { echo "❌ $*" >&2; exit 1; }
code() { curl -sS -o /tmp/lsm-acc-body -w '%{http_code}' "$@"; }
expect_code() { [ "$2" == "$3" ] || fail "$1: 期望状态码 $2，实际 $3（body: $(cat /tmp/lsm-acc-body 2>/dev/null || true)）"; }
expect_contains() { [[ "$2" == *"$3"* ]] || fail "$1: 期望响应包含 $3，实际 $2"; }
j() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))" "$1"; }

start() { # start <data_dir> <log>  -> 等待就绪
  "$BIN" --addr "127.0.0.1:${PORT}" --data-dir "$1" --max-mem 10000 >"$2" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 100); do
    if code "$BASE/admin/state" >/dev/null 2>&1 && [ "$(code "$BASE/admin/state")" == "200" ]; then return; fi
    kill -0 "$SRV_PID" 2>/dev/null || fail "服务提前退出，日志：$2"
    sleep 0.1
  done
  fail "服务未在 10s 内就绪，日志：$2"
}

put() { curl -sS -X PUT "$BASE/kv/$1" -H 'Content-Type: application/json' -d "{\"value\":\"$2\"}" >/dev/null; }

trap 'kill "$SRV_PID" 2>/dev/null || true' EXIT

if [ ! -x "$BIN" ]; then
  echo "未找到 release 二进制 $BIN，先执行: cargo build --release"
  exit 1
fi

step '首次启动；k=v1 刷盘并逐级合并到 L2'
start "$DATA" "$LOG1"
put k v1
curl -sS -X POST "$BASE/admin/flush" >/dev/null
curl -sS -X POST "$BASE/admin/compact?level=0" >/dev/null
curl -sS -X POST "$BASE/admin/compact?level=1" >/dev/null

step 'k=v2 刷盘，合并到 L1'
put k v2
curl -sS -X POST "$BASE/admin/flush" >/dev/null
curl -sS -X POST "$BASE/admin/compact?level=0" >/dev/null

step 'k=v3（停在 L0）及邻近键 a/m/z，整体刷盘'
put k v3; put a a1; put m m1; put z z1
curl -sS -X POST "$BASE/admin/flush" >/dev/null
CODE=$(code "$BASE/kv/k"); expect_code '删除前点查 k' 200 "$CODE"
expect_contains '删除前读到 v3' "$(cat /tmp/lsm-acc-body)" '"value":"v3"'

step '删除 k（墓碑）并刷盘'
expect_code '删除 k' 200 "$(code -X DELETE "$BASE/kv/k")"
curl -sS -X POST "$BASE/admin/flush" >/dev/null
expect_code '删除后点查 k' 404 "$(code "$BASE/kv/k")"

MANIFEST_BEFORE="$(cat "$DATA/manifest.json")"

step '故障注入：合并 L0+L1 写完新段后以 exit(42) 崩溃（manifest 不切换）'
set +e
curl -sS -X POST "$BASE/admin/compact?level=0&fault=exit_after_write" >/dev/null 2>&1
wait "$SRV_PID"; RC=$?
set -e
[ "$RC" == "42" ] || fail "期望崩溃退出码 42，实际 $RC（日志：$LOG1）"
echo "进程已按预期崩溃（exit 42）"
ls "$DATA" | grep -E '^seg-.*\.jsonl$' | sed 's/^/  磁盘段文件: /'

step '用同一数据目录重启：恢复清单、清理孤儿段'
start "$DATA" "$LOG2"
expect_code '重启后旧值不复活' 404 "$(code "$BASE/kv/k")"

BODY="$(curl -sS "$BASE/range")"
echo "$BODY"
[ "$(echo "$BODY" | j 'd["count"]')" == "3" ] || fail '范围应有 3 个键'
[ "$(echo "$BODY" | j '[i["key"] for i in d["items"]]')" == "['a', 'm', 'z']" ] \
  || fail '范围结果应恰好为 a,m,z（无重复、无墓碑键 k）'

step '校验 manifest 未被失败的合并改写'
STATE_IDS="$(code "$BASE/admin/state" >/dev/null; cat /tmp/lsm-acc-body | j 'sorted([s["id"] for s in d["segments"]])')"
MF_IDS="$(echo "$MANIFEST_BEFORE" | j 'sorted([s["id"] for s in d["segments"]])')"
[ "$STATE_IDS" == "$MF_IDS" ] || fail "清单被改写：$STATE_IDS != $MF_IDS"

step '恢复后合并 L0+L1：L2 仍有同键，墓碑保留'
BODY="$(curl -sS -X POST "$BASE/admin/compact?level=0")"; echo "$BODY"
[ "$(echo "$BODY" | j 'd["tombstones_kept"]')" == "1" ] || fail '墓碑应保留 1 个'
expect_code '合并后旧值仍不复活' 404 "$(code "$BASE/kv/k")"

step '合并 L1+L2：确认更老层无同键，墓碑安全丢弃'
BODY="$(curl -sS -X POST "$BASE/admin/compact?level=1")"; echo "$BODY"
[ "$(echo "$BODY" | j 'd["tombstones_dropped"]')" == "1" ] || fail '墓碑应丢弃 1 个'
expect_code '丢弃墓碑后旧值仍不复活' 404 "$(code "$BASE/kv/k")"
BODY="$(curl -sS "$BASE/range")"
[ "$(echo "$BODY" | j '[i["key"] for i in d["items"]]')" == "['a', 'm', 'z']" ] || fail '最终范围结果应为 a,m,z'

step '第二次重启：最终状态持久化'
kill "$SRV_PID"; wait "$SRV_PID" 2>/dev/null || true
start "$DATA" "$LOG3"
expect_code '再次重启后旧值仍不复活' 404 "$(code "$BASE/kv/k")"
BODY="$(curl -sS "$BASE/range")"
[ "$(echo "$BODY" | j 'd["count"]')" == "3" ] || fail '最终范围仍应为 3 个键'

echo
echo '✅ 全部验收通过：跨三层覆盖+删除、合并中断(exit 42)、重启恢复、旧值不复活、范围无重复、墓碑先留后弃。'
echo "   数据目录: $DATA（可手动检查）；服务日志: $LOG1 $LOG2 $LOG3"
kill "$SRV_PID" 2>/dev/null || true
trap - EXIT
