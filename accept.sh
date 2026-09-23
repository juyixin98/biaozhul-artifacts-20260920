#!/usr/bin/env bash
# accept.sh —— 两阶段提交恢复模拟器的自动化验收脚本。
# 依次执行：单元/集成测试 -> 构建 -> 12 个场景 -> 无部分提交断言
#          -> 阻塞证据断言 -> 跨进程恢复。所有报告输出到 reports/。
#
# 退出码：0 全部通过；非 0 有验收项失败（失败项打印到 stderr 与 reports/summary.txt）。
set -u
cd "$(dirname "$0")"

OUT=reports
rm -rf "$OUT"
mkdir -p "$OUT"
BIN=bin/twopcsim
FAIL=0

log()  { printf '%s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*" | tee -a "$OUT/summary.txt" >&2; FAIL=1; }
pass() { printf 'PASS  %s\n' "$*" | tee -a "$OUT/summary.txt"; }

log "== 1. go vet =="
if go vet ./...; then pass "go vet"; else fail "go vet"; fi

log "== 2. go test（含 -race）=="
if go test -race -count=1 ./... 2>&1 | tee "$OUT/go-test.txt"; then
  pass "go test ./..."
else
  fail "go test ./..."
fi

log "== 3. 构建 =="
if go build -o "$BIN" ./cmd/twopcsim; then pass "go build"; else fail "go build"; exit 1; fi

# run_scenario <file> 运行并输出报告
run_scenario() {
  local f="$1" name
  name=$(basename "$f" .json)
  "$BIN" -scenario "$f" -out "$OUT/$name.json"
  echo "$name"
}

# assert_outcome <report> <txnId> <expected verdict>
assert_outcome() {
  local rep="$1" txn="$2" want="$3"
  python3 - "$rep" "$txn" "$want" <<'EOF'
import json,sys
rep,txn,want=sys.argv[1:4]
r=json.load(open(rep))
o=next((x for x in r['outcomes'] if x['txnId']==txn), None)
got=o['verdict'] if o else '<missing>'
if got!=want:
    print(f"verdict mismatch for {txn}: got={got} want={want}")
    sys.exit(1)
if r['verdict']=='partial-commit-detected':
    print("partial commit detected at report level")
    sys.exit(1)
EOF
}

log "== 4. 12 个场景逐一运行并断言裁决 =="
declare -A WANT=(
  [01-happy-path]="committed"
  [02-lossy-network]="committed"
  [03-coord-crash-after-start]="committed"
  [04-coord-crash-after-commit]="committed"
  [05-coord-down-forever-blocked]="blocked"
  [06-coord-down-before-decision-blocked]="blocked"
  [07-participant-crash-prepared]="committed"
  [08-participant-crash-at-commit]="committed"
  [09-vote-no-abort]="aborted"
  [10-coord-crash-after-abort]="aborted"
  [11-multi-txn-duplicates]="committed"
  [12-participant-crash-before-prepared-timeout-abort]="aborted"
)
for f in examples/0*.json examples/1*.json; do
  [ -e "$f" ] || continue
  name=$(basename "$f" .json)
  run_scenario "$f" >/dev/null
  code=$?
  # CLI 退出码 0 表示无部分提交；blocked/aborted 也应是 0。
  if [ "$code" != "0" ]; then fail "$name (CLI exit=$code)"; continue; fi
  if assert_outcome "$OUT/$name.json" "txn-A" "${WANT[$name]}" 2>>"$OUT/summary.txt"; then
    pass "$name -> ${WANT[$name]}"
  else
    fail "$name (期望 ${WANT[$name]})"
  fi
done

# 11 号场景第二事务必须 aborted。
if assert_outcome "$OUT/11-multi-txn-duplicates.json" "txn-B" "aborted" 2>>"$OUT/summary.txt"; then
  pass "11 multi-txn: txn-B -> aborted"
else
  fail "11 multi-txn: txn-B 期望 aborted"
fi

log "== 5. 无部分提交（全局）=="
if grep -l '"partialCommit": true' "$OUT"/*.json >/dev/null 2>&1; then
  fail "存在部分提交: $(grep -l '\"partialCommit\": true' "$OUT"/*.json | xargs -n1 basename | tr '\n' ' ')"
else
  pass "全部场景无部分提交"
fi

log "== 6. 阻塞必须有持锁+询问证据，且参与者不得自行回滚 =="
python3 - "$OUT/05-coord-down-forever-blocked.json" "$OUT/06-coord-down-before-decision-blocked.json" <<'EOF'
import json,sys
bad=[]
for path in sys.argv[1:]:
    r=json.load(open(path))
    o=r['outcomes'][0]
    be=o.get('blockedEvidence')
    if not be: bad.append(f"{path}: 缺少 blockedEvidence"); continue
    locked=[p for p in be['participants'] if p['lockHeld'] and p['waitedTicks']>0 and p['queriesSent']>=2]
    if len(locked)!=3: bad.append(f"{path}: 持锁阻塞参与者不足3个: {len(locked)}")
    if be['coordinatorAvailable']: bad.append(f"{path}: 协调者应不可用")
    # trace 中参与者不得自行 abort
    for e in r['trace']:
        if e['node'].startswith('participant') and e['kind'] in ('participant.abort.fromPeer',):
            bad.append(f"{path}: 参与者自行回滚 @tick{e['tick']}")
if bad:
    print("\n".join(bad)); sys.exit(1)
print("阻塞证据完整（3 参与者持锁、周期询问、无自行回滚）")
EOF
if [ $? -eq 0 ]; then pass "阻塞证据断言"; else fail "阻塞证据断言"; fi

log "== 7. 跨进程恢复（同一数据目录，第二个进程）=="
SHARED="$OUT/resume-data"
rm -rf "$SHARED"
python3 - "$SHARED" <<'EOF'
import json,sys
sc=json.load(open('examples/05-coord-down-forever-blocked.json'))
sc['dataDir']=sys.argv[1]
json.dump(sc,open(sys.argv[1]+'.first.json','w'))
sc2={
  "name":"resume-process-2","seed":1,"maxTick":300,
  "dataDir":sys.argv[1],"fresh":False,
  "network":{"minDelay":1,"maxDelay":3},
  "timings":{"voteTimeout":60,"resend":12,"query":8}
}
json.dump(sc2,open(sys.argv[1]+'.second.json','w'))
EOF
"$BIN" -scenario "$SHARED.first.json"  -out "$OUT/resume-run1.json"
"$BIN" -scenario "$SHARED.second.json" -out "$OUT/resume-run2.json"
python3 - "$OUT/resume-run1.json" "$OUT/resume-run2.json" <<'EOF'
import json,sys
r1=json.load(open(sys.argv[1])); r2=json.load(open(sys.argv[2]))
v1=r1['outcomes'][0]['verdict']; v2=r2['outcomes'][0]['verdict']
c=len(r2['outcomes'][0].get('committed') or [])
print(f"进程1裁决={v1}（协调者永久不可用）; 进程2恢复后裁决={v2}, committed={c}")
if v1!='blocked' or v2!='committed' or c!=3:
    sys.exit("跨进程恢复未收敛到全员提交")
EOF
if [ $? -eq 0 ]; then pass "跨进程恢复 blocked -> committed"; else fail "跨进程恢复"; fi

log ""
if [ "$FAIL" -eq 0 ]; then
  log "================ 全部验收通过 ================"
  echo "ALL ACCEPTANCE CHECKS PASSED" | tee -a "$OUT/summary.txt"
else
  log "================ 存在失败项，见 reports/summary.txt ================"
fi
exit $FAIL
