#!/usr/bin/env bash
# 端到端演示：构建服务 -> 启动 -> 依次跑验收场景 -> 保存输出。
# 依赖：bash、curl、python3（用于回填上次缓存键和美化 JSON）。
set -euo pipefail

cd "$(dirname "$0")/.."
BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
OUT_DIR="examples/results"
mkdir -p "$OUT_DIR"

echo "==> cargo build --release"
cargo build --release

PORT="${PORT:-0}"
if [ "$PORT" = "0" ]; then
  # 让 OS 分配一个空闲端口：bind 一次再取出。
  PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
fi
BASE_URL="http://127.0.0.1:${PORT}"
echo "    (chosen port: $PORT)"

echo "==> starting server on ${BASE_URL}"
BIND_ADDR="127.0.0.1:${PORT}" ./target/release/incr-build-planner >"$OUT_DIR/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT

# 等服务就绪
for _ in $(seq 1 50); do
  if curl -sf "$BASE_URL/health" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
curl -s "$BASE_URL/health" > "$OUT_DIR/00_health.json"
python3 -m json.tool "$OUT_DIR/00_health.json"
echo

# python 助手：用冷构建响应里的 build_keys 回填后续请求的 previous.build_keys。
# 参数：$1 = 冷构建响应文件，$2 = 模板请求文件；stdout = 填充后 JSON。
fill_keys() {
  python3 - "$1" "$2" <<'PY'
import json, sys
resp = json.load(open(sys.argv[1]))
req = json.load(open(sys.argv[2]))
keys = {nid: b["cache_key"] for nid, b in resp["builds"].items()}
prev = req.get("previous")
if prev is not None:
    prev["build_keys"] = {k: keys[k] for k in prev.get("build_keys", {}) if k in keys}
json.dump(req, sys.stdout, indent=2)
PY
}

run_scenario() {
  local name="$1" template="$2"
  echo "==> scenario: $name"
  fill_keys "$OUT_DIR/01_cold_build.json" "$template" \
    | curl -s -X POST "$BASE_URL/plan" \
        -H 'content-type: application/json' \
        --data-binary @- > "$OUT_DIR/${name}.json"
  python3 -m json.tool "$OUT_DIR/${name}.json" \
    | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("    steps                :", r.get("steps"))
print("    full_order           :", r.get("full_order"))
print("    timestamp_only_files :", r.get("timestamp_only_files"))
print("    unrelated_files      :", r.get("unrelated_files"))
print("    stats                :", r.get("stats"))
if "cycles" in r:
    print("    cycles               :", r["cycles"])
'
  echo
}

# 01 冷构建（全量基准）
echo "==> scenario: 01 cold build (full baseline)"
curl -s -X POST "$BASE_URL/plan" -H 'content-type: application/json' \
  --data-binary @examples/01_cold_build.json > "$OUT_DIR/01_cold_build.json"
python3 -m json.tool "$OUT_DIR/01_cold_build.json" \
  | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("    steps      :", r["steps"])
print("    full_order :", r["full_order"])
print("    cold_build :", r["cold_build"])
'
echo

# 02 改一个叶输入：只应重建 compile_l + link（一次）
run_scenario "02_change_leaf" examples/02_change_leaf.json

# 03 只 touch：零动作
run_scenario "03_touch_only" examples/03_touch_only.json

# 04 工具版本变化：compile_r + link
run_scenario "04_tool_version_change" examples/04_tool_version_change.json

# 05 无关文件：零动作
run_scenario "05_unrelated_file" examples/05_unrelated_file.json

# 06 环定位（/validate，200 + valid:false）
echo "==> scenario: 06 cycle locate (validate)"
curl -s -X POST "$BASE_URL/validate" -H 'content-type: application/json' \
  --data-binary @examples/06_cycle_validate.json > "$OUT_DIR/06_cycle.json"
python3 -m json.tool "$OUT_DIR/06_cycle.json"

echo
echo "==> results written to $OUT_DIR/"
echo "==> done"
