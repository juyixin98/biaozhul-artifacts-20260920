#!/usr/bin/env bash
# 端到端冒烟：启动 JSON 服务 -> 打各端点 -> 关闭。
# 用法: bash examples/requests/curl_smoke.sh [port]
set -euo pipefail
PORT="${1:-8765}"
BASE="http://127.0.0.1:${PORT}"
DIR="$(cd "$(dirname "$0")" && pwd)"

python3 -m ssa_tool.service --host 127.0.0.1 --port "${PORT}" &
PID=$!
trap 'kill ${PID} 2>/dev/null || true' EXIT

# 等服务起来
for i in $(seq 1 50); do
  curl -sf "${BASE}/health" >/dev/null && break
  sleep 0.1
done

echo "== /health =="
curl -s "${BASE}/health"
echo; echo "== /pipeline (diamond, a=4, 期望返回 14) =="
curl -s -X POST "${BASE}/pipeline" \
  -H 'Content-Type: application/json' \
  --data @"${DIR}/pipeline_diamond.json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("ok:",d["ok"]); print("执行:",d["execution"]); print("SSA违规:",d["ssa_violations"])'

echo "== /ssa (loop_sum, 看 φ) =="
curl -s -X POST "${BASE}/ssa" \
  -H 'Content-Type: application/json' \
  --data @"${DIR}/ssa_loop.json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("ok:",d["ok"],"违规:",d["violations"]); print("\n".join(l for l in d["text"].splitlines() if "phi" in l))'

echo "== /interpret (nested, exec flavor) =="
curl -s -X POST "${BASE}/interpret" \
  -H 'Content-Type: application/json' \
  --data @"${DIR}/interpret_nested.json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d)'

echo "== 错误请求 (未声明变量) =="
curl -s -X POST "${BASE}/parse" -H 'Content-Type: application/json' \
  -d '{"source":"func main(){ y = 1; }"}'
echo
echo "冒烟完成"
