#!/usr/bin/env bash
# acceptance.sh — 工作窃取执行器的端到端验收脚本
#
# 做三件事：
#   1) go test ./...（含随机取消、单线程深递归等全部自动化测试）
#   2) 启动 -workers 1 的服务，提交一棵很深的递归树（饥饿死锁探针）
#      并校验节点计数；然后提交大量任务并随机取消一部分
#   3) 通过 /events 事件流校验：每个任务至多 started 一次、恰好一个终态
#
# 用法: scripts/acceptance.sh [PORT]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# Pick an ephemeral loopback port ourselves (python is required later
# anyway), so a stale/foreign process cannot make us talk to the wrong
# server.
PORT="${1:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')}"
BASE="http://127.0.0.1:${PORT}"
EVENT_LOG="$(mktemp -t wsd-acceptance-XXXXXX.jsonl)"
BIN="$ROOT/bin/wsd"

echo "=================================================================="
echo " 步骤 1/4: go vet + go test -race ./..."
echo "=================================================================="
go vet ./...
go test -race ./... -count=1

echo
echo "=================================================================="
echo " 步骤 2/4: 构建并以单线程模式 (-workers 1) 启动服务"
echo "=================================================================="
go build -o "$BIN" ./cmd/wsd
"$BIN" -addr "127.0.0.1:${PORT}" -workers 1 -event-log "$EVENT_LOG" \
  > /tmp/wsd-acceptance.log 2>&1 &
PID=$!
trap 'kill -TERM $PID 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "wsd 进程启动失败，日志：" >&2; cat /tmp/wsd-acceptance.log >&2; exit 1
  fi
  if curl -sf "$BASE/healthz" 2>/dev/null | grep -q '"workers"'; then break; fi
  sleep 0.1
done
if ! curl -sf "$BASE/healthz" 2>/dev/null | grep -q '"workers"'; then
  echo "wsd 健康检查失败，日志：" >&2; cat /tmp/wsd-acceptance.log >&2; exit 1
fi
echo "healthz: $(curl -s "$BASE/healthz")"

submit() { # kind payload -> id
  curl -s -X POST "$BASE/tasks" -H 'Content-Type: application/json' \
    -d "{\"kind\":\"$1\",\"payload\":$2}" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["task"]["id"])'
}
state_of() {
  curl -s "$BASE/tasks/$1" | python3 -c 'import json,sys;print(json.load(sys.stdin)["task"]["state"])'
}
wait_terminal() { # id [timeout_sec]
  local id="$1"
  local timeout="${2:-15}"
  local deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local st; st="$(state_of "$id")"
    case "$st" in
      succeeded|failed|panicked|canceled) echo "$st"; return 0;;
    esac
    sleep 0.05
  done
  echo "TIMEOUT" >&2; return 1
}

echo
echo "=================================================================="
echo " 步骤 3/4: 单线程深递归树 (depth=11 -> 4095 个任务), 校验无饥饿死锁"
echo "=================================================================="
ROOT_ID="$(submit recurse '{"depth":11}')"
ST="$(wait_terminal "$ROOT_ID" 30)"
echo "root $ROOT_ID -> $ST"
test "$ST" = "succeeded"
COUNT=$(curl -s "$BASE/tasks/$ROOT_ID" \
  | python3 -c 'import json,sys;print(int(json.load(sys.stdin)["task"]["value"]))')
echo "子树节点数=$COUNT (期望 4095)"
test "$COUNT" -eq 4095

echo
echo "=================================================================="
echo " 步骤 4/4: 随机取消 + 事件流不变量 (至多一次/恰好一个终态/可退出)"
echo "=================================================================="
# 交错提交与取消：每提交一棵较深的树，立即随机决定是否取消，使取消
# 一定能命中 pending 或 running 中的任务（单 worker 下很多树仍在排队）。
IDS=()
CANCEL_COUNT=0
for i in $(seq 1 16); do
  d=$(( RANDOM % 4 + 10 ))
  id="$(submit recurse "{\"depth\":$d}")"
  IDS+=( "$id" )
  if [ $(( RANDOM % 2 )) -eq 0 ]; then
    curl -s -X POST "$BASE/tasks/$id/cancel" >/dev/null || true
    CANCEL_COUNT=$((CANCEL_COUNT+1))
  fi
done
echo "提交 ${#IDS[@]} 棵任务树，其中 $CANCEL_COUNT 棵请求取消"
for id in "${IDS[@]}"; do wait_terminal "$id" 60 >/dev/null; done

# 再排空一点时间，让所有（含被取消的）子任务落终态
for _ in $(seq 1 50); do
  OUT=$(curl -s "$BASE/stats" | python3 -c 'import json,sys;s=json.load(sys.stdin)["stats"];print(s["outstanding"],s["running"])')
  [ "$OUT" = "0 0" ] && break
  sleep 0.1
done

echo "最终 stats:"
curl -s "$BASE/stats" | python3 -m json.tool

echo
echo "-- JSONL 事件不变量校验 ($EVENT_LOG) --"
python3 - "$EVENT_LOG" <<'PY'
import json, sys, collections
path = sys.argv[1]
started = collections.Counter()
completed = collections.Counter()
states = {}
seqs = []
with open(path) as f:
    for line in f:
        e = json.loads(line)
        seqs.append(e["seq"])
        t = e.get("task_id")
        if not t:
            continue
        if e["type"] == "task.started":
            started[t] += 1
        elif e["type"] == "task.completed":
            completed[t] += 1
            assert t not in states, f"{t} reached two terminal states"
            states[t] = e["state"]

assert all(a < b for a, b in zip(seqs, seqs[1:])), "event seq not strictly increasing"
multi = {t: n for t, n in started.items() if n > 1}
assert not multi, f"tasks executed more than once: {multi}"
assert all(n == 1 for n in completed.values()), "some task completed != 1 times"
assert set(started) | set(completed) == set(states), "terminal set mismatch"
print(f"tasks with terminal state: {len(states)}")
print("max starts per task: ", max(started.values(), default=0))
dist = dict(collections.Counter(states.values()))
print("state distribution:   ", dist)
assert dist.get("canceled", 0) > 0, "no task was canceled — cancellation not exercised"
print("ALL EVENT INVARIANTS HOLD: at-most-once execution, exactly-one terminal state")
PY

# 优雅关闭：所有任务已排空，Shutdown 应快速返回且进程退出
echo
echo "-- 优雅关闭 (POST /shutdown) --"
curl -s -X POST "$BASE/shutdown"
for _ in $(seq 1 100); do
  if ! kill -0 "$PID" 2>/dev/null; then break; fi
  sleep 0.1
done
if kill -0 "$PID" 2>/dev/null; then
  echo "服务未能在超时内退出" >&2; exit 1
fi
trap - EXIT
echo "服务已正常退出。"
echo
echo "✅ 验收全部通过：单线程深递归无死锁、随机取消、至多执行一次、最终可退出。"
