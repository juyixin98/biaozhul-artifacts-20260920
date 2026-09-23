#!/usr/bin/env bash
# 简易负载生成器：并发向同一模型发送 N 个请求，观察批次数与延迟。
# 用法: N=50 bash examples/load.sh
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8080}
N=${N:-20}
MODEL=${MODEL:-llm-load}

pids=()
start_ns=$(date +%s%N)
for i in $(seq 1 "$N"); do
  curl --noproxy "*" -sS -o /dev/null -X POST "$BASE/v1/infer" \
    -H 'Content-Type: application/json' \
    -d "{\"id\":\"load-$i\",\"model\":\"$MODEL\",\"prompt\":\"prompt number $i\"}" &
  pids+=($!)
done
wait "${pids[@]}"
end_ns=$(date +%s%N)
elapsed_ms=$(( (end_ns - start_ns) / 1000000 ))

echo "submitted=$N elapsed=${elapsed_ms}ms"
curl --noproxy "*" -fsS "$BASE/v1/metrics" | python3 -c '
import sys, json
m = json.load(sys.stdin)
s, e = m["scheduler"], m["executor"]
print("flushed:", s["flushed"], "reasons:", s["flush_reasons"])
print("executor batch sizes:", e["batch_sizes"])
'
