#!/usr/bin/env bash
# 端到端冒烟脚本：启动服务 -> 摄入/生成各类样例 -> 查询 -> 关闭。
# 用法: ./examples/requests.sh [BASE_URL]
set -euo pipefail
BASE="${1:-http://127.0.0.1:8080}"
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== health =="
curl -fsS "$BASE/healthz" | j

echo "== 摄入串并行 demo trace =="
curl -fsS -X POST "$BASE/v1/traces/manual-demo/spans" \
  -H 'Content-Type: application/json' \
  --data @examples/ingest-demo.json | j

echo "== 查询关键路径 =="
curl -fsS "$BASE/v1/traces/manual-demo/critical-path" | j

echo "== 列出全部 trace =="
curl -fsS "$BASE/v1/traces" | j

echo "== 内置合成大 trace（6 个分片，部分并行）=="
curl -fsS -X POST "$BASE/v1/sample/synth?trace_id=synth-1&n=6" | j

echo "== 重叠区间（时钟矛盾告警，HTTP 200 + warning）=="
curl -fsS -X POST "$BASE/v1/sample/overlap?trace_id=ov-1" | j

echo "== 缺 span + 越界（warning）=="
curl -fsS -X POST "$BASE/v1/sample/missing?trace_id=miss-1" | j

echo "== 循环输入（HTTP 422 + error 诊断）=="
curl -sS -o /tmp/cycle-resp.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE/v1/sample/cycle?trace_id=cy-1"
cat /tmp/cycle-resp.json | j
