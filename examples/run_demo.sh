#!/usr/bin/env bash
# 端到端验收演示：启动真实 HTTP 服务，用 curl 对照“增量构建 vs 全量构建”。
#
# 覆盖验收点：
#   1) 冷启动全量构建（菱形合并节点只执行一次）
#   2) 修改叶输入内容 -> 沿边传播，且增量产物 == 全量产物
#   3) 仅修改时间戳 -> 零重建
#   4) 修改无关文件 -> 忽略，零重建
#   5) 修改构建规则（命令）
#   6) 修改工具版本与声明环境 -> 缓存键覆盖，触发重建
#   7) 依赖环 -> 422，可定位到具体节点/边
#
# 依赖：bash、curl、jq。用法：bash examples/run_demo.sh
set -uo pipefail

PORT="${PORT:-8090}"
BASE="http://127.0.0.1:${PORT}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/.." && pwd)"
TMP="$(mktemp -d)"
trap 'kill "${SERVER_PID:-}" 2>/dev/null; rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
check() { # check <描述> <实际> <期望>
  if [[ "$2" == "$3" ]]; then echo "  PASS: $1 (= $2)"; PASS=$((PASS+1));
  else echo "  FAIL: $1 (实际=$2 期望=$3)"; FAIL=$((FAIL+1)); fi
}

echo ">> cargo build"
cargo build --quiet --manifest-path "$REPO/Cargo.toml" || { echo "构建失败"; exit 1; }

PORT="$PORT" "$REPO/target/debug/incremental-build-planner" >"$TMP/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done
echo ">> 服务已启动: $BASE (pid $SERVER_PID)"

# 菱形图：a.c -> b.o, a.c -> c.o, (b.o,c.o) -> app
graph() { # graph <gcc_version> <ld_command> <cc_env>
  cat <<JSON
{
  "nodes": [
    { "id": "a.c", "type": "input" },
    { "id": "b.o", "type": "target", "depends_on": ["a.c"],
      "rule": { "command": "gcc -c a.c -o b.o",
                "tool": { "name": "gcc", "version": "$1" },
                "environment": { "CC": "$3", "CFLAGS": "-O2" } } },
    { "id": "c.o", "type": "target", "depends_on": ["a.c"],
      "rule": { "command": "gcc -c a.c -o c.o",
                "tool": { "name": "gcc", "version": "$1" },
                "environment": { "CC": "$3", "CFLAGS": "-O2" } } },
    { "id": "app", "type": "target", "depends_on": ["b.o", "c.o"],
      "rule": { "command": "$2",
                "tool": { "name": "ld", "version": "2.42" },
                "environment": {} } }
  ]
}
JSON
}
files() { # files <hash> <mtime> [extra_file]
  local extra="${3:-}"
  jq -n --arg h "$1" --argjson m "$2" --arg e "$extra" '
    { "a.c": { content_hash: $h, mtime: $m } }
    + (if $e == "" then {} else { ($e): { content_hash: "deadbeef", mtime: 9 } } end)'
}
req() { # req <graph_json> <files_json> [baseline_file]
  jq -n --argjson g "$1" --argjson f "$2" --slurpfile b "${3:-/dev/null}" '
    { graph: $g, files: $f } + (if ($b|length) > 0 then { baseline: $b[0] } else { baseline: null } end)'
}

echo; echo "== 1) 冷启动：全量构建，菱形合并节点只一次 =="
R=$(req "$(graph 13.2.0 'ld b.o c.o -o app' gcc)" "$(files h1 1000)" | \
    curl -s -w '\n%{http_code}' -H 'content-type: application/json' -d @- "$BASE/plan")
CODE=$(<<<"$R" tail -1); BODY=$(<<<"$R" sed '$d')
check "http 状态码" "$CODE" 200
check "status" "$(<<<"$BODY" jq -r .status)" full_build
check "增量步骤数" "$(<<<"$BODY" jq -r .incremental.step_count)" 3
check "全量步骤数" "$(<<<"$BODY" jq -r .full_build.step_count)" 3
check "app 出现次数" "$(<<<"$BODY" jq -r '[.incremental.steps[].node]|map(select(.=="app"))|length')" 1
check "模拟等价" "$(<<<"$BODY" jq -r .simulation.equivalent)" true
<<<"$BODY" jq .next_snapshot > "$TMP/snap1.json"
echo "  步骤: $(<<<"$BODY" jq -rc '.incremental.steps[].node' | paste -sd, -)"

echo; echo "== 2) 修改叶输入内容（h1 -> h2，时间也变）=="
R=$(req "$(graph 13.2.0 'ld b.o c.o -o app' gcc)" "$(files h2 1001)" "$TMP/snap1.json" | \
    curl -s -w '\n%{http_code}' -H 'content-type: application/json' -d @- "$BASE/plan")
CODE=$(<<<"$R" tail -1); BODY=$(<<<"$R" sed '$d')
check "http 状态码" "$CODE" 200
check "变更类型" "$(<<<"$BODY" jq -r .changed_inputs[0].kind)" content_changed
check "timestamp_only 数" "$(<<<"$BODY" jq -r '.timestamp_only_inputs|length')" 0
check "重建步骤" "$(<<<"$BODY" jq -rc '[.incremental.steps[].node]|join(",")')" "b.o,c.o,app"
check "app 只执行一次" "$(<<<"$BODY" jq -r '[.incremental.steps[].node]|map(select(.=="app"))|length')" 1
check "增量==全量产物" "$(<<<"$BODY" jq -r '.simulation.incremental_artifacts == .simulation.full_artifacts')" true
check "模拟等价" "$(<<<"$BODY" jq -r .simulation.equivalent)" true
<<<"$BODY" jq .next_snapshot > "$TMP/snap2.json"

echo; echo "== 3) 仅修改时间戳（内容哈希仍为 h2）=="
R=$(req "$(graph 13.2.0 'ld b.o c.o -o app' gcc)" "$(files h2 7777)" "$TMP/snap2.json" | \
    curl -s -w '\n%{http_code}' -H 'content-type: application/json' -d @- "$BASE/plan")
CODE=$(<<<"$R" tail -1); BODY=$(<<<"$R" sed '$d')
check "变更类型" "$(<<<"$BODY" jq -r .changed_inputs[0].kind)" timestamp_only
check "重建步骤数" "$(<<<"$BODY" jq -r .incremental.step_count)" 0
check "跳过数" "$(<<<"$BODY" jq -r .incremental.skipped_count)" 3
check "缓存命中有效" "$(<<<"$BODY" jq -r .simulation.cache_hits_valid)" true
check "增量==全量产物" "$(<<<"$BODY" jq -r '.simulation.incremental_artifacts == .simulation.full_artifacts')" true

echo; echo "== 4) 修改无关文件（多带 README.md）=="
R=$(req "$(graph 13.2.0 'ld b.o c.o -o app' gcc)" "$(files h2 1001 README.md)" "$TMP/snap2.json" | \
    curl -s -w '\n%{http_code}' -H 'content-type: application/json' -d @- "$BASE/plan")
CODE=$(<<<"$R" tail -1); BODY=$(<<<"$R" sed '$d')
check "重建步骤数" "$(<<<"$BODY" jq -r .incremental.step_count)" 0
check "ignored_files" "$(<<<"$BODY" jq -rc .ignored_files)" '["README.md"]'

echo; echo "== 5) 修改构建规则（app 的链接命令）=="
R=$(req "$(graph 13.2.0 'ld b.o c.o -o app-v2' gcc)" "$(files h2 1001)" "$TMP/snap2.json" | \
    curl -s -w '\n%{http_code}' -H 'content-type: application/json' -d @- "$BASE/plan")
CODE=$(<<<"$R" tail -1); BODY=$(<<<"$R" sed '$d')
check "重建步骤" "$(<<<"$BODY" jq -rc '[.incremental.steps[].node]|join(",")')" app
check "规则差异" "$(<<<"$BODY" jq -r .rule_changes[0].details[0])" command_changed
check "增量==全量产物" "$(<<<"$BODY" jq -r '.simulation.incremental_artifacts == .simulation.full_artifacts')" true

echo; echo "== 6) 修改工具版本(13.2.0->14.1.0) 与声明环境(CC=gcc->clang) =="
R=$(req "$(graph 14.1.0 'ld b.o c.o -o app' clang)" "$(files h2 1001)" "$TMP/snap2.json" | \
    curl -s -w '\n%{http_code}' -H 'content-type: application/json' -d @- "$BASE/plan")
CODE=$(<<<"$R" tail -1); BODY=$(<<<"$R" sed '$d')
check "重建步骤" "$(<<<"$BODY" jq -rc '[.incremental.steps[].node]|join(",")')" "b.o,c.o,app"
DETAILS="$(<<<"$BODY" jq -rc '[.rule_changes[].details[]] | unique | join(",")')"
check "规则差异含工具版本/环境" "$DETAILS" "environment_changed,tool_version_changed"
check "增量==全量产物" "$(<<<"$BODY" jq -r '.simulation.incremental_artifacts == .simulation.full_artifacts')" true

echo; echo "== 7) 依赖环：b.o -> app -> b.o =="
CYCLE='{"nodes":[
  {"id":"a.c","type":"input"},
  {"id":"b.o","type":"target","depends_on":["a.c","app"],
   "rule":{"command":"x","tool":{"name":"gcc","version":"1"},"environment":{}}},
  {"id":"app","type":"target","depends_on":["b.o"],
   "rule":{"command":"y","tool":{"name":"ld","version":"1"},"environment":{}}}
]}'
R=$(req "$CYCLE" "$(files h1 1)" | \
    curl -s -w '\n%{http_code}' -H 'content-type: application/json' -d @- "$BASE/plan")
CODE=$(<<<"$R" tail -1); BODY=$(<<<"$R" sed '$d')
check "http 状态码" "$CODE" 422
check "错误类型" "$(<<<"$BODY" jq -r .error)" cycle_detected
check "环路径首尾" "$(<<<"$BODY" jq -r '.cycle.nodes[0] == .cycle.nodes[-1]')" true
echo "  环定位: $(<<<"$BODY" jq -rc '.cycle.nodes|join(" -> ")')"
echo "  环上的边: $(<<<"$BODY" jq -rc '.cycle.edges[]|"\(.from)->\(.to)"' | paste -sd',')"

echo; echo "== 汇总: PASS=$PASS FAIL=$FAIL =="
[[ "$FAIL" -eq 0 ]]
