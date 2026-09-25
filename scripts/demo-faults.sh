#!/usr/bin/env bash
# 端到端验收：对每个故障点运行“崩溃 + 恢复”，并与无故障连续执行基线比较最终偏移与汇总。
#
# 用法: scripts/demo-faults.sh [--halt]
#   默认以“抛出 InjectedCrash”建模崩溃（退出码 99，进程仍由脚本重启）；
#   --halt 使用 Runtime.halt(99) 真正杀掉 JVM（不运行 shutdown hook），更接近 kill -9。
set -uo pipefail
cd "$(dirname "$0")/.."

HALT=false
if [ "${1:-}" = "--halt" ]; then HALT=true; fi

./scripts/build.sh --all >/dev/null
CP="build/classes"
CLI=(java -cp "$CP" dev.example.cp.cli.Cli)
WORK=build/demo
rm -rf "$WORK"

EVENTS=examples/events.json
EVERY=5                       # 每 5 条一个 epoch：epoch1=偏移0..4，epoch2=偏移5..9
POINTS=(PENDING_WRITE STATE_WRITE COMMIT_RENAME TABLE_APPLY AFTER_COMMIT)

field() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d[sys.argv[2]])" "$1" "$2"; }

# ---------- 1) 无故障连续执行基线 ----------
BASE="$WORK/base"
"${CLI[@]}" init --data "$BASE" >/dev/null
"${CLI[@]}" load --data "$BASE" --file "$EVENTS" >/dev/null
"${CLI[@]}" run  --data "$BASE" --every "$EVERY" >"$WORK/base.run.log"
"${CLI[@]}" status --data "$BASE" >"$WORK/base.status.json"
BASE_OFFSET=$(field "$WORK/base.status.json" committedOffset)
BASE_SUMS=$(field "$WORK/base.status.json" sums)
echo "baseline(continuous): committedOffset=$BASE_OFFSET sums=$BASE_SUMS"
echo

PASS=0; FAIL=0
for POINT in "${POINTS[@]}"; do
  D="$WORK/$POINT"
  "${CLI[@]}" init --data "$D" >/dev/null
  "${CLI[@]}" load --data "$D" --file "$EVENTS" >/dev/null

  # epoch 1 干净完成（只处理前 5 条，--every 5 自动检查点后停止）
  "${CLI[@]}" run --data "$D" --every "$EVERY" --max-events 5 >"$D.run1.log"

  # 在 epoch 2 的指定点安排一次性故障
  if [ "$HALT" = true ]; then
    "${CLI[@]}" fault --data "$D" --at "$POINT" --epoch 2 --halt >/dev/null
  else
    "${CLI[@]}" fault --data "$D" --at "$POINT" --epoch 2 >/dev/null
  fi

  # 继续处理剩余 5 条 —— 必然崩溃
  set +e
  "${CLI[@]}" run --data "$D" --every "$EVERY" >"$D.crash.log" 2>&1
  RC=$?
  set -e

  # 崩溃后、恢复前的只读现场
  "${CLI[@]}" peek --data "$D" >"$D.peek.json"
  PEEK_EPOCH=$(field "$D.peek.json" appliedEpoch)
  PEEK_OFF=$(field "$D.peek.json" committedOffset)

  # 重新运行 = 进程重启 + 自动恢复
  "${CLI[@]}" run --data "$D" --every "$EVERY" >"$D.recover.log" 2>&1
  "${CLI[@]}" status --data "$D" >"$D.status.json"
  OFF=$(field "$D.status.json" committedOffset)
  SUMS=$(field "$D.status.json" sums)

  if [ "$RC" -ne 0 ] && [ "$OFF" = "$BASE_OFFSET" ] && [ "$SUMS" = "$BASE_SUMS" ]; then
    RESULT="OK  "; PASS=$((PASS+1))
  else
    RESULT="BAD "; FAIL=$((FAIL+1))
  fi
  printf "%s %-15s crashExit=%s afterCrash(epoch=%s,off=%s) -> recovered off=%s sums=%s\n" \
    "$RESULT" "$POINT" "$RC" "$PEEK_EPOCH" "$PEEK_OFF" "$OFF" "$SUMS"
done

echo
echo "result: $PASS passed, $FAIL failed (halt=$HALT)"
[ "$FAIL" -eq 0 ]
