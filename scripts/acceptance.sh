#!/usr/bin/env bash
# 端到端验收脚本：
#   1. cargo build / cargo test
#   2. 启动本地 HTTP 服务，用小内存强制多轮归并
#   3. 对照 in-memory 参考排序（extsort verify）
#   4. 覆盖空输入、超大单行、临时文件损坏、故障后恢复
#
# 用法: bash scripts/acceptance.sh
# 退出码: 0 全部通过；非 0 有未通过项。
set -u
cd "$(dirname "$0")/.."

ROOT="$(pwd)/.acceptance"
REPO="$ROOT/repo"
PORT="${PORT:-18099}"
ADDR="127.0.0.1:$PORT"
BIN=./target/debug/extsort
PASS=0
FAIL=0

ok()   { echo "PASS: $*"; PASS=$((PASS+1)); }
bad()  { echo "FAIL: $*"; FAIL=$((FAIL+1)); }

echo "== 清理工作区 =="
rm -rf "$ROOT"
mkdir -p "$REPO"

echo "== cargo build =="
cargo build || { echo "build failed"; exit 1; }

echo "== cargo test =="
if cargo test; then ok "cargo test"; else bad "cargo test"; fi

echo "== 生成数据 =="
python3 - <<'PY'
import os, random
random.seed(42)
root = ".acceptance"
os.makedirs(root, exist_ok=True)
groups = ["alpha","bravo","charlie","delta","echo","foxtrot","golf","hotel"]
# 平均约 150B/行 x 1200 行 ~ 180KB；mem=2048/buf=256 时会产生大量 run 和多轮归并
with open(f"{root}/input.tsv", "w") as f:
    for i in range(1200):
        g = random.choice(groups)
        f.write(f"{g}\t{random.randrange(7):05}\tid-{i:06}-{'x'*random.randrange(80,140)}\n")
# 空输入
open(f"{root}/empty.txt", "w").close()
# 超大单行（300KB，远超 mem=2048），夹杂若干普通行
with open(f"{root}/oversize.txt", "w") as f:
    for i in range(10):
        f.write(f"small\t{i:03}\n")
    f.write("huge\t" + "Z" * 300_000 + "\n")
    for i in range(10):
        f.write(f"small\t{i:03}\n")
print("data generated")
PY

echo "== 启动服务 =="
$BIN serve "$ADDR" "$REPO" >"$ROOT/server.log" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for i in $(seq 1 50); do
  curl -sS "http://$ADDR/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -sS "http://$ADDR/healthz" | grep -q ok && ok "healthz" || bad "healthz"

post() { # job mem buf key file
  local job="$1" mem="$2" buf="$3" key="$4" file="$5"
  curl -sS -D "$ROOT/$job.headers" -o "$ROOT/$job.out" \
    -X POST --data-binary @"$file" \
    "http://$ADDR/sort?job=$job&mem=$mem&buf=$buf&key=$key"
}

echo "== 小内存多轮归并，对照内存参考排序 =="
post multi 2048 256 "1%3Aasc%3B2%3Aasc" "$ROOT/input.tsv" || true
grep -q "200 OK" "$ROOT/multi.headers" && ok "multi: HTTP 200" || bad "multi: HTTP status"
echo "--- 响应头 ---"; grep -i '^x-sort' "$ROOT/multi.headers"
RUNS=$(grep -i '^x-sort-runs:' "$ROOT/multi.headers" | tr -dc '0-9')
ROUNDS=$(grep -i '^x-sort-merge-rounds:' "$ROOT/multi.headers" | tr -dc '0-9')
[ "${RUNS:-0}" -ge 10 ] && ok "multi: runs=$RUNS (>=10)" || bad "multi: runs=$RUNS 太少"
[ "${ROUNDS:-0}" -ge 2 ] && ok "multi: merge rounds=$ROUNDS (>=2)" || bad "multi: rounds=$ROUNDS 不足两轮"
$BIN verify "$ROOT/input.tsv" "$ROOT/multi.out" "1:asc;2:asc" \
  && ok "multi: 与内存参考排序一致" || bad "multi: 与参考排序不一致"

echo "== 幂等：GET 重放已完成作业 =="
curl -sS -D "$ROOT/multi2.headers" -o "$ROOT/multi2.out" \
  "http://$ADDR/sort?job=multi&mem=2048&buf=256&key=1%3Aasc%3B2%3Aasc"
cmp "$ROOT/multi.out" "$ROOT/multi2.out" && ok "GET resume 输出一致" || bad "GET resume 输出不一致"

echo "== 空输入 =="
post empty 2048 256 "" "$ROOT/empty.txt"
[ ! -s "$ROOT/empty.out" ] && ok "empty: 输出为空" || bad "empty: 输出非空"
$BIN verify "$ROOT/empty.txt" "$ROOT/empty.out" "" && ok "empty: verify" || bad "empty: verify"

echo "== 超大单行 =="
post big 2048 256 "1%3Aasc" "$ROOT/oversize.txt"
grep -q "200 OK" "$ROOT/big.headers" && ok "oversize: HTTP 200" || bad "oversize: HTTP"
$BIN verify "$ROOT/oversize.txt" "$ROOT/big.out" "1:asc" \
  && ok "oversize: 与参考一致（300KB 单行可排序）" || bad "oversize: 与参考不一致"

echo "== 配置冲突返回 409 =="
code=$(curl -sS -o /dev/null -w '%{http_code}' \
  "http://$ADDR/sort?job=multi&mem=2048&buf=256&key=1%3Adesc")
[ "$code" = "409" ] && ok "conflict -> 409" || bad "conflict -> $code"

echo "== 损坏已落盘分段：CRC 必须检测到，重跑可恢复 =="
RUNFILE=$(ls "$REPO/jobs/big/runs/"*.exs | head -1)
# 损坏前必须校验通过
$BIN verify-run "$RUNFILE" >/dev/null && ok "run 文件完好时 verify-run 通过" || bad "verify-run 误报损坏"
python3 - "$RUNFILE" <<'PY'
import sys
p = sys.argv[1]
data = bytearray(open(p,'rb').read())
data[40] ^= 0xFF
open(p,'wb').write(data)
print("corrupted", p)
PY
if $BIN verify-run "$RUNFILE" >/dev/null 2>"$ROOT/corrupt.err"; then
  bad "损坏 run 未被 CRC 检出"
else
  ok "损坏 run 被 CRC/聚合校验检出 ($(cat "$ROOT/corrupt.err"))"
fi
# 通过 HTTP 重跑损坏作业，应自动重建并成功。
curl -sS -o "$ROOT/big2.out" -w '%{http_code}\n' \
  "http://$ADDR/sort?job=big&mem=2048&buf=256&key=1%3Aasc" > "$ROOT/big2.code"
[ "$(cat "$ROOT/big2.code")" = "200" ] && ok "corrupt run: 重跑恢复 200" || bad "corrupt run: $(cat "$ROOT/big2.code")"
cmp "$ROOT/big.out" "$ROOT/big2.out" && ok "corrupt run: 恢复后输出一致" || bad "corrupt run: 输出不一致"

echo "== 遗留临时文件清理 =="
echo stale > "$REPO/jobs/empty/runs/run-000999.tmp"
code=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST --data-binary @"$ROOT/empty.txt" \
  "http://$ADDR/sort?job=empty2&mem=2048&buf=256")
[ "$code" = "200" ] && ok "stale tmp cleaned" || bad "stale tmp -> $code"

echo
echo "================ 结果 ================"
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
