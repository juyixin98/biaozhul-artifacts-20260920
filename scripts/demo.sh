#!/usr/bin/env bash
# 端到端演示：CPU 密集租户 alpha 与内存密集租户 beta。
# 演示 DRF 放置、资源守恒（用 python 自动断言）、确定性平局与释放后重调度。
#
# 用法：
#   ./scripts/demo.sh            # 自动在 :19097 启动服务，结束后关闭
#   BASE=http://127.0.0.1:8080 ./scripts/demo.sh   # 使用已运行的服务
set -euo pipefail

BASE="${BASE:-}"
STARTED=""
CLEANUP_BIN=""
if [[ -z "$BASE" ]]; then
  BASE="http://127.0.0.1:19097"
  echo ">> 构建并启动服务: $BASE"
  BIN="$(mktemp -t drf-demo.XXXXXX)"
  CLEANUP_BIN="$BIN"
  go build -o "$BIN" ./cmd/server
  "$BIN" -addr ":19097" >/tmp/drf-demo-server.log 2>&1 &
  SRV_PID=$!
  STARTED="yes"
  for _ in $(seq 1 50); do
    curl -sf "$BASE/healthz" >/dev/null && break
    sleep 0.1
  done
  trap '[[ -n "$STARTED" ]] && kill "$SRV_PID" 2>/dev/null || true; [[ -n "$CLEANUP_BIN" ]] && rm -f "$CLEANUP_BIN"' EXIT
fi

api() { # method path [json]
  if [[ -n "${3:-}" ]]; then
    curl -sS -o - -X "$1" -H 'Content-Type: application/json' -d "$3" "$BASE$2"
  else
    curl -sS -o - -X "$1" "$BASE$2"
  fi
}

show() { # 标题 + JSONPath（如 "state.num_queued"），从 stdin 读 JSON
  echo
  echo "== $1 =="
  python3 -c '
import json, sys
path = sys.argv[1]
cur = json.load(sys.stdin)
for p in path.split("."):
    if p:
        cur = cur[p]
print(json.dumps(cur, indent=2, ensure_ascii=False))
' "$2"
}

# 校验资源守恒：used + free == capacity，且每租户 alloc <= quota。
assert_conservation() {
  local state
  state="$(api GET /state)"
  STATE="$state" python3 - <<'PY'
import json, os, sys
st = json.loads(os.environ["STATE"])
cap, used, free = st["capacity"], st["used"], st["free"]
for dim in ("cpu", "mem"):
    assert used[dim] + free[dim] == cap[dim], f"conservation broken on {dim}"
    assert used[dim] <= cap[dim], f"overcommit on {dim}"
for t in st["tenants"]:
    a, q = t["allocated"], t["quota"]
    assert a["cpu"] <= q["cpu"] and a["mem"] <= q["mem"], f"quota breached: {t['id']}"
    sc = sum(task["cpu"] for task in t["running"])
    sm = sum(task["mem"] for task in t["running"])
    assert (sc, sm) == (a["cpu"], a["mem"]), f"allocation mismatch: {t['id']}"
print("conservation OK: used=(%d,%d) free=(%d,%d)" % (used["cpu"], used["mem"], free["cpu"], free["mem"]))
PY
}

echo ">> 重置并创建租户：alpha(CPU 密集)、beta(内存密集)，权重均为 1"
api POST /admin/reset >/dev/null
# 顺手演示权重既接受数字也接受精确分数（"1/3"）：建一个临时租户再删除。
api POST /tenants '{"id":"tmp","weight":"1/3"}' | show "权重支持精确分数（临时租户）" "weight"
api DELETE /tenants/tmp >/dev/null
api POST /tenants '{"id":"alpha","weight":1}' | show "租户 alpha" "id"
api POST /tenants '{"id":"beta","weight":1}' | show "租户 beta" "id"

echo ">> beta 提交内存密集任务 b1=(1000cpu,6000mem)、b2 同型"
api POST /tasks '{"id":"b1","tenant":"beta","cpu":1000,"mem":6000}' | show "b1" "status"
api POST /tasks '{"id":"b2","tenant":"beta","cpu":1000,"mem":6000}' | show "b2（内存不足，排队）" "status"

echo ">> alpha 提交 CPU 密集任务 a1=(6000cpu,1000mem)"
api POST /tasks '{"id":"a1","tenant":"alpha","cpu":6000,"mem":1000}' | show "a1（beta 队首放不下，跳过；alpha 放得下）" "status"
echo ">> a2 同型：平局 ID 偏向 alpha，但集群只剩 3000 CPU -> 排队"
api POST /tasks '{"id":"a2","tenant":"alpha","cpu":6000,"mem":1000}' | show "a2" "status"

api GET /state | show "全局状态（用量/份额/队列）" ""
assert_conservation

echo
echo ">> 释放 b1：beta 份额归零，b2 被立即重调度"
api DELETE /tasks/b1 | show "释放 b1 后的 beta 队列" "state.tenants"
assert_conservation

echo ">> 释放 b2：只剩 a1，空闲 4000 CPU，a2 仍差 2000 CPU -> 继续排队"
api DELETE /tasks/b2 | show "释放 b2" "state.num_queued"
assert_conservation

echo ">> 释放 a1：a2 立即得到资源运行"
api DELETE /tasks/a1 | show "释放 a1 后 a2 状态" "state"
assert_conservation

echo
echo ">> 演示完成"
