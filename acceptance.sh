#!/usr/bin/env bash
# =============================================================================
# 验收脚本：在“状态写入”和“输出提交”的各个点注入故障，比较连续执行与恢复执行
# 的最终偏移和汇总。所有崩溃/恢复都通过独立 JVM 进程完成（退出码 42=注入崩溃）。
#
# 阶段：STATE_WRITE / OUTPUT_STAGE / OUTPUT_COMMIT / TABLE_APPLY
# 比较：nextOffset、lastCheckpointId、summary、已提交事务集合
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p docs logs
LOG=logs/acceptance-$(date +%Y%m%dT%H%M%S).log
exec > >(tee "$LOG") 2>&1

EVERY=5
EVENTS=examples/events.json
CRASH_AT=4
PASS=0; FAIL=0

say() { echo; echo "==== $* ===="; }

./build.sh

# --- 归一化 status：只取验收关心的确定性字段（剔除时间戳、armedFaults 等易变量） ---
normalize() {
  jq -S '{nextOffset, lastCheckpointId, committedTxns, summary}' "$1"
}

say "1) 连续执行基线（无故障，全新进程）"
rm -rf data/baseline
java -cp build com.example.cptx.Main baseline --dir data/baseline --events "$EVENTS" --every $EVERY
java -cp build com.example.cptx.Main status --dir data/baseline --every $EVERY > /tmp/base-status.json
normalize /tmp/base-status.json > /tmp/base-norm.json
cat /tmp/base-norm.json

run_phase() {
  local phase="$1"
  say "2) 故障阶段 ${phase}：崩溃（进程退出码 42）→ 全新进程恢复"
  rm -rf "data/crash-${phase}"
  java -cp build com.example.cptx.Main crash --dir "data/crash-${phase}" \
      --events "$EVENTS" --every $EVERY --fault "${phase}:${CRASH_AT}"
  local rc=$?
  echo "[crash] 退出码=$rc（期望 42）"
  [ "$rc" = "42" ] || { echo "!! 崩溃退出码不是 42"; FAIL=$((FAIL+1)); return; }

  # 崩溃现场证据
  echo "[crash] 崩溃后磁盘现场（.tmp/.staged 为尚未提交的证据，恢复时会清除/回滚）："
  find "data/crash-${phase}" \( -name '*.tmp' -o -name '*.staged' \) 2>/dev/null | sed 's/^/    /' || true

  java -cp build com.example.cptx.Main resume --dir "data/crash-${phase}" --every $EVERY
  java -cp build com.example.cptx.Main status --dir "data/crash-${phase}" --every $EVERY > "/tmp/${phase}-status.json"
  normalize "/tmp/${phase}-status.json" > "/tmp/${phase}-norm.json"

  if diff -u /tmp/base-norm.json "/tmp/${phase}-norm.json" > "/tmp/${phase}.diff"; then
    echo "[${phase}] 最终偏移与汇总与基线完全一致 ✓"
    PASS=$((PASS+1))
  else
    echo "[${phase}] 与基线不一致 ✗，diff："; cat "/tmp/${phase}.diff"
    FAIL=$((FAIL+1))
  fi

  # 事务文件编号集合必须与基线相同
  local baseTxns crashTxns
  baseTxns=$(jq -r '.committedTxns[]' /tmp/base-norm.json | tr '\n' ' ')
  crashTxns=$(jq -r '.committedTxns[]' "/tmp/${phase}-norm.json" | tr '\n' ' ')
  if [ "$baseTxns" = "$crashTxns" ]; then
    echo "[${phase}] 已提交事务集合一致：{$crashTxns} ✓（无重复、无缺失）"
  else
    echo "[${phase}] 事务集合不一致：基线 {$baseTxns} vs 恢复 {$crashTxns} ✗"
    FAIL=$((FAIL+1))
  fi

  # 恢复后不得残留暂存/临时文件
  if find "data/crash-${phase}" \( -name '*.tmp' -o -name '*.staged' \) | grep -q .; then
    echo "[${phase}] 恢复后仍有 .tmp/.staged 残留 ✗"; FAIL=$((FAIL+1))
  else
    echo "[${phase}] 恢复后无 .tmp/.staged 残留 ✓"
  fi
}

for phase in STATE_WRITE OUTPUT_STAGE OUTPUT_COMMIT TABLE_APPLY; do
  run_phase "$phase"
done

say "3) 双故障（跨三个进程）：STATE_WRITE@3 崩溃 → OUTPUT_COMMIT@6 崩溃 → 再恢复"
rm -rf data/crash-DOUBLE
java -cp build com.example.cptx.Main crash --dir data/crash-DOUBLE \
    --events "$EVENTS" --every $EVERY --fault "STATE_WRITE:3"; [ $? = 42 ] || FAIL=$((FAIL+1))
java -cp build com.example.cptx.Main resume --dir data/crash-DOUBLE \
    --every $EVERY --fault "OUTPUT_COMMIT:6"; rc1=$?
echo "[crash2] 退出码=$rc1（期望 42）"; [ "$rc1" = "42" ] || FAIL=$((FAIL+1))
java -cp build com.example.cptx.Main resume --dir data/crash-DOUBLE --every $EVERY
java -cp build com.example.cptx.Main status --dir data/crash-DOUBLE --every $EVERY > /tmp/DOUBLE-status.json
normalize /tmp/DOUBLE-status.json > /tmp/DOUBLE-norm.json
if diff -q /tmp/base-norm.json /tmp/DOUBLE-norm.json >/dev/null; then
  echo "[DOUBLE] 两次崩溃后最终偏移与汇总仍与基线一致 ✓"; PASS=$((PASS+1))
else
  echo "[DOUBLE] 双故障后与基线不一致 ✗"; diff -u /tmp/base-norm.json /tmp/DOUBLE-norm.json; FAIL=$((FAIL+1))
fi

say "4) 汇总表可从提交日志独立重建（删掉 summary.json 后重启）"
rm -rf data/rebuild; cp -r data/baseline data/rebuild
rm -f data/rebuild/summary.json
java -cp build com.example.cptx.Main status --dir data/rebuild --every $EVERY > /tmp/rebuild-status.json
normalize /tmp/rebuild-status.json > /tmp/rebuild-norm.json
if diff -q /tmp/base-norm.json /tmp/rebuild-norm.json >/dev/null; then
  echo "[REBUILD] summary.json 删除后由已提交事务日志重建，结果一致 ✓"; PASS=$((PASS+1))
else
  echo "[REBUILD] 重建结果不一致 ✗"; FAIL=$((FAIL+1))
fi

say "验收结果：通过 $PASS 项，失败 $FAIL 项"
echo "完整日志：$LOG"
[ "$FAIL" = 0 ]
