#!/usr/bin/env bash
# 一键本地演示：启动 Anvil → 编译 → 部署 → 启动 API → 用 curl 走一遍领取/重放场景。
# 用法: bash scripts/demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.foundry/bin:$PATH"

ANVIL_PORT="${ANVIL_PORT:-8555}"
API_PORT="${API_PORT:-8022}"
RPC="http://127.0.0.1:${ANVIL_PORT}"
API="http://127.0.0.1:${API_PORT}"
KEY0="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

echo "==> 1/6 编译合约"
forge build >/dev/null

echo "==> 2/6 启动 Anvil (端口 ${ANVIL_PORT})"
anvil --port "${ANVIL_PORT}" --chain-id 31337 >/tmp/anvil.demo.log 2>&1 &
ANVIL_PID=$!
trap 'kill ${ANVIL_PID} ${API_PID:-} 2>/dev/null || true' EXIT
sleep 2

echo "==> 3/6 部署合约（预测 CREATE 地址→绑定地址生成根→部署），并预存 20 ETH"
.venv/bin/python -m deploy.deploy \
  --rpc "${RPC}" \
  --allocations examples/allocations.json --fund-eth 20 \
  --out deployments/anvil.json | tee /tmp/deploy.json
ADDR=$(.venv/bin/python -c "import json;print(json.load(open('deployments/anvil.json'))['address'])")

echo "==> 4/6 启动 FastAPI (端口 ${API_PORT})"
RPC_URL="${RPC}" CONTRACT_ADDRESS="${ADDR}" PRIVATE_KEY="${KEY0}" \
  ALLOCATIONS_FILE=examples/allocations.json \
  .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port "${API_PORT}" \
  --log-level warning >/tmp/api.demo.log 2>&1 &
API_PID=$!
sleep 3

echo "==> 5/6 正常批量领取索引 [0,1,2,3]"
curl -s -X POST "${API}/batch/submit" \
  -H 'content-type: application/json' \
  -d '{"indices":[0,1,2,3]}' | .venv/bin/python -m json.tool

echo "==> 6/6 重复索引重放 [1,4]（应被链上回滚，API 返回 400，索引 4 不留部分领取）"
curl -s -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST \
  "${API}/batch/submit" -H 'content-type: application/json' \
  -d '{"indices":[1,4]}'
cat /tmp/resp.json | .venv/bin/python -m json.tool || true

echo "索引 4 是否已领（期望 false）:"
cast call "${ADDR}" "isClaimed(uint256)(bool)" 4 --rpc-url "${RPC}"

echo "演示完成。"
