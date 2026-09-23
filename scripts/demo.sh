#!/usr/bin/env bash
# 完全本地的支付通道全流程演示：
#   部署 → 开通 → 链下更新(两次签名状态) → 用旧状态关闭 → 挑战(更高序号) → 到期结算
# 依赖：foundry (forge/cast/anvil)。无需联网。
set -euo pipefail
cd "$(dirname "$0")/.."

RPC="${RPC_URL:-http://127.0.0.1:8545}"
PAYER=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266   # anvil 账户 #0
PAYEE=0x70997970C51812dc3A010C7d01b50e0d17dc79C8   # anvil 账户 #1

# 1. 启动本地链（若已在运行则复用）
if cast block-number --rpc-url "$RPC" >/dev/null 2>&1; then
  echo "== 复用已运行的节点: $RPC"
else
  echo "== 启动 anvil 本地链"
  anvil --silent >/tmp/p038-anvil.log 2>&1 &
  ANVIL_PID=$!
  trap 'kill "$ANVIL_PID" 2>/dev/null || true' EXIT
  sleep 1
fi

step() { echo; echo "== $*"; }

step "部署 TestToken + PaymentChannel"
forge script script/Deploy.s.sol --rpc-url "$RPC" --broadcast -q

step "开通通道（抵押 100 TT）"
forge script script/Open.s.sol --rpc-url "$RPC" --broadcast -q

step "链下更新：双方签名状态 nonce=1 (30 TT)"
cp examples/state-1.json examples/state-input.json
forge script script/Update.s.sol --rpc-url "$RPC" -q
cp examples/state-signed.json examples/state-1-signed.json

step "链下更新：双方签名状态 nonce=2 (70 TT)"
cp examples/state-2.json examples/state-input.json
forge script script/Update.s.sol --rpc-url "$RPC" -q
cp examples/state-signed.json examples/state-2-signed.json

step "用旧状态 nonce=1 (30 TT) 关闭通道，进入挑战期"
cp examples/state-1-signed.json examples/state-signed.json
forge script script/ChannelOps.s.sol:Close --rpc-url "$RPC" --broadcast -q

step "挑战：提交更高序号状态 nonce=2 (70 TT)"
cp examples/state-2-signed.json examples/state-signed.json
forge script script/ChannelOps.s.sol:Challenge --rpc-url "$RPC" --broadcast -q

step "快进到挑战期结束（+90000 秒）"
cast rpc evm_increaseTime 90000 --rpc-url "$RPC" >/dev/null
cast rpc evm_mine --rpc-url "$RPC" >/dev/null

step "结算"
forge script script/ChannelOps.s.sol:Settle --rpc-url "$RPC" --broadcast -q

TOKEN=$(grep -o '"token": *"0x[0-9a-fA-F]*"' deployments/local.json | grep -o '0x[0-9a-fA-F]*')
step "最终余额（应为 payee=70, payer=999930，单位 1e18）"
echo -n "payer: "; cast call "$TOKEN" "balanceOf(address)(uint256)" "$PAYER" --rpc-url "$RPC"
echo -n "payee: "; cast call "$TOKEN" "balanceOf(address)(uint256)" "$PAYEE" --rpc-url "$RPC"
echo; echo "演示完成。"
