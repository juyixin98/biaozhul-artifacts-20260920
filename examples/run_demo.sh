#!/usr/bin/env bash
# 端到端演示：启动服务 → 配置集群 → 放置/拒绝/释放 全场景。
# 用法: ./examples/run_demo.sh  (需要 go 与 curl)
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-18080}"
BASE="http://127.0.0.1:${PORT}"
BIN="$(mktemp -d)/gpu-placement"

echo "==> 构建服务"
go build -o "$BIN" .

GPU_PLACEMENT_ADDR="127.0.0.1:${PORT}" "$BIN" &
SRV_PID=$!
trap 'kill $SRV_PID 2>/dev/null || true' EXIT

for i in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

step() { echo; echo "==> $1"; }

req() { # req METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  echo "--- $method $path ${body:+(请求体: $body)}"
  if [ -n "$body" ]; then
    curl -s -X "$method" -H 'Content-Type: application/json' -d "$body" "$BASE$path"
  else
    curl -s -X "$method" "$BASE$path"
  fi
  echo
}

step "1. 健康检查"
req GET /healthz

step "2. 配置 8 卡 / 2 NUMA 集群（含 4 对 NVLink 直连）"
req PUT /api/cluster "$(cat examples/cluster.json)"

step "3. 放置 2 卡训练任务 → 应选中 NVLink 直连的 gpu0+gpu1（cost=0）"
req POST /api/tasks "$(cat examples/task-2gpu.json)"

step "4. 放置 4 卡任务 → 整体落在 NUMA0（gpu0..gpu3，0 个跨 NUMA 对）"
req POST /api/tasks '{"taskId":"train-4gpu","replicas":4,"memoryPerReplicaMB":8192}'

step "5. 再放一个 4 卡任务 → NUMA0 显存未占满，仍整体落 NUMA0（显存可共享）"
req POST /api/tasks '{"taskId":"train-4gpu-b","replicas":4,"memoryPerReplicaMB":8192}'

step "6. 第三个 4 卡任务 → NUMA0 的 gpu0/gpu1 已满，整体落 NUMA1（仍 0 个跨 NUMA 对）"
req POST /api/tasks '{"taskId":"train-4gpu-c","replicas":4,"memoryPerReplicaMB":8192}'

step "7. 无解：申请 9 张卡 → NOT_ENOUGH_DEVICES"
req POST /api/tasks '{"taskId":"too-many","replicas":9,"memoryPerReplicaMB":1024}'

step "8. 无解：申请 8×24GB（共 192GB > 剩余总量）→ INSUFFICIENT_MEMORY"
req POST /api/tasks '{"taskId":"too-big","replicas":8,"memoryPerReplicaMB":24576}'

step "9. 查看集群状态（每卡已用/剩余显存）"
req GET /api/cluster

step "10. 释放全部任务，换一个小集群演示显存碎片"
req DELETE /api/tasks/train-llm
req DELETE /api/tasks/train-4gpu
req DELETE /api/tasks/train-4gpu-b
req DELETE /api/tasks/train-4gpu-c
req PUT /api/cluster '{"devices":[
  {"id":"gpu0","memoryMB":24576,"numaNode":0},
  {"id":"gpu1","memoryMB":24576,"numaNode":0}]}'

step "11. 小任务占走 gpu0 的 12GB（gpu0 剩 12GB，gpu1 剩 24GB）"
req POST /api/tasks "$(cat examples/task-1gpu-12g.json)"

step "12. 申请 2×16GB：总剩余 36GB >= 32GB，但单卡 >=16GB 的只有 1 台 → FRAGMENTED_MEMORY"
req POST /api/tasks "$(cat examples/task-fragmented.json)"

step "13. 释放小任务后碎片消失，同样的请求成功"
req DELETE /api/tasks/train-shard
req POST /api/tasks '{"taskId":"train-frag","replicas":2,"memoryPerReplicaMB":16384}'

step "14. 任务列表"
req GET /api/tasks

echo
echo "==> 演示完成，关闭服务"
