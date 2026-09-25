#!/usr/bin/env bash
# demo.sh — 端到端演示：乱序、缺根超时、迟到补全、重复冲突、时钟偏差、父链循环。
# 使用确定性入口 POST /admin/traces/<id>/flush 触发超时，不依赖真实等待。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ADDR="127.0.0.1:18080"
DEMO_DIR="$(mktemp -d)"
trap 'kill ${SERVER_PID:-} 2>/dev/null || true; rm -rf "$DEMO_DIR"' EXIT

cd "$ROOT_DIR"
go build -o "$DEMO_DIR/tracestitch" ./cmd/tracestitch

pick_port() {
  for p in $(shuf -i 20000-30000 -n 20 2>/dev/null || seq 20000 20020); do
    if ! (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null; then
      echo "$p"; return 0
    fi
    exec 3>&- 2>/dev/null || true
  done
  echo 20000
}
PORT="$(pick_port)"
ADDR="127.0.0.1:$PORT"
echo "==> data dir: $DEMO_DIR, port $PORT"
"$DEMO_DIR/tracestitch" -addr "$ADDR" -data-dir "$DEMO_DIR" -trace-timeout 30s -sweep-interval 1s &
SERVER_PID=$!

base="http://$ADDR"
wait_http() {
  for _ in $(seq 1 50); do
    if curl -sf "$base/healthz" >/dev/null; then return 0; fi
    sleep 0.1
  done
  echo "server did not become ready" >&2
  exit 1
}
wait_http

j() { jq -C "$@"; }

echo
echo "############ 1) 乱序：先摄入孙/子（根缺失） ############"
curl -s -X POST "$base/v1/spans" -H 'Content-Type: application/json' \
  -d @"$SCRIPT_DIR/01-out-of-order-children.json" | j .
echo "--- 当前 trace（缺根，不完整；pay/checkout 为孤儿挂载在森林尾部） ---"
curl -s "$base/v1/traces/demo-ooo" | j '{complete, latest: .latestRevision | {version, reason, complete, missingRoot, hasOrphans}}'

echo
echo "############ 2) 重复冲突：同一 spanId 不同载荷，首条为准 ############"
curl -s -X POST "$base/v1/span" -H 'Content-Type: application/json' \
  -d @"$SCRIPT_DIR/03-duplicate-conflict.json" | j .
curl -s "$base/v1/traces/demo-ooo" | j '{conflicts, paySpanName: (.spans[] | select(.spanId=="pay-svc") | .name)}'

echo
echo "############ 3) 强制超时：缺根 -> timeout 不完整修订 ############"
curl -s -X POST "$base/admin/traces/demo-ooo/flush" | j '{sealed, latest: .latestRevision | {version, reason, complete, missingRoot, spanIds}}'

echo
echo "############ 4) 根迟到：生成 late 修订并补全 ############"
curl -s -X POST "$base/v1/span" -H 'Content-Type: application/json' \
  -d @"$SCRIPT_DIR/02-late-root.json" | j .
echo "--- 包含关系核对：holds 必须为 true ---"
curl -s "$base/v1/traces/demo-ooo/containment" | j .
echo "--- 最终拼装树（因果只看引用：gateway->checkout-svc->pay-svc） ---"
curl -s "$base/v1/traces/demo-ooo" | j '{complete, sealed, forest: [.forest[] | {span: .span.spanId, children: [.children[] | {span: .span.spanId, children: [.children[] | .span.spanId]}]}], revisions: [.revisions[] | {version, reason, complete, spanIds}]}'

echo
echo "############ 5) 跨服务时钟偏差：结构按引用，偏差仅告警 ############"
curl -s -X POST "$base/v1/spans" -H 'Content-Type: application/json' \
  -d @"$SCRIPT_DIR/04-clock-skew.json" >/dev/null
curl -s "$base/v1/traces/demo-skew" | j '{complete, treeRoot: .forest[0].span.spanId, treeChild: .forest[0].children[0].span.spanId, skew: .latestRevision.clockSkew}'

echo
echo "############ 6) 父链循环：a->b->c->a 与 d 自环 ############"
curl -s -X POST "$base/v1/spans" -H 'Content-Type: application/json' \
  -d @"$SCRIPT_DIR/05-parent-cycle.json" >/dev/null
curl -s "$base/v1/traces/demo-cycle" | j '{complete, hasCycles: .latestRevision.hasCycles, cyclePath: .latestRevision.cyclePath}'

echo
echo "############ 7) 缺根的另一条 trace：超时不完整 ############"
curl -s -X POST "$base/v1/spans" -H 'Content-Type: application/json' \
  -d @"$SCRIPT_DIR/06-missing-root.json" >/dev/null
curl -s -X POST "$base/admin/traces/demo-missing-root/flush" | j '{sealed, latest: .latestRevision | {reason, complete, missingRoot}}'

echo
echo "############ 8) 持久化产物 ############"
echo "--- data/wal.jsonl 事件数 ---"
wc -l "$DEMO_DIR/data/wal.jsonl"
echo "--- 快照文件 ---"
ls -1 "$DEMO_DIR/data/snapshots/"

echo
echo "demo 完成。"
