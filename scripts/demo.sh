#!/usr/bin/env bash
#
# Replayable end-to-end demonstration on a local Anvil node.
#
# Boots an ephemeral Anvil (or reuses $RPC), deploys the contracts, then runs:
#   bootstrap liquidity -> second proportional deposit -> swap with
#   amountOutMin/deadline -> partial withdrawal -> full withdrawal.
# After every action it verifies reserves == real token balances.
#
# Usage:
#   scripts/demo.sh
#   RPC=http://127.0.0.1:8545 scripts/demo.sh   # reuse a running node
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.foundry/bin:$PATH"
RPC="${RPC:-http://127.0.0.1:8545}"
ETH=1000000000000000000
MAX_UINT=115792089237316195423570985008687907853269984665640564039457584007913129639935
# bash integers are 64-bit; build 18-decimal token amounts as decimal strings.
wad() { printf '%s%s' "$1" "000000000000000000"; }

# Anvil deterministic account #0 / #1.
ALICE=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
AK=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
BOB=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
BK=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d

ANVIL_PID=""
if ! cast block-number --rpc-url "$RPC" >/dev/null 2>&1; then
  echo ">> starting ephemeral anvil on $RPC"
  anvil --silent --port 8545 >/tmp/cpmm-anvil.log 2>&1 &
  ANVIL_PID=$!
  trap 'kill "$ANVIL_PID" 2>/dev/null || true' EXIT
  for _ in $(seq 1 50); do
    cast block-number --rpc-url "$RPC" >/dev/null 2>&1 && break
    sleep 0.1
  done
fi

s()  { cast send  --rpc-url "$RPC" --private-key "$1" "${@:2}"; }
q()  { cast call  --rpc-url "$RPC" "${@}"; }
# cast prints big decimals as "123 [1.2e2]" and hex values plainly; normalize.
dec(){ sed -E 's/[[:space:]]*\[.*\]$//' <<<"$1"; }
dl() { echo $(( $(date +%s) + 3600 )); }

cd "$ROOT"
echo ">> building"
forge build >/dev/null

echo ">> deploying factory, router, two test tokens"
FACTORY=$(forge create src/CPMMFactory.sol:CPMMFactory --rpc-url "$RPC" --private-key "$AK" --broadcast | awk '/Deployed to:/{print $3}')
ROUTER=$(forge create src/CPMMRouter.sol:CPMMRouter --rpc-url "$RPC" --private-key "$AK" --broadcast --constructor-args "$FACTORY" | awk '/Deployed to:/{print $3}')
TA=$(forge create src/mocks/TestERC20.sol:TestERC20 --rpc-url "$RPC" --private-key "$AK" --broadcast --constructor-args "Test Token A" "TKA" | awk '/Deployed to:/{print $3}')
TB=$(forge create src/mocks/TestERC20.sol:TestERC20 --rpc-url "$RPC" --private-key "$AK" --broadcast --constructor-args "Test Token B" "TKB" | awk '/Deployed to:/{print $3}')

echo "   factory=$FACTORY"
echo "   router =$ROUTER"
echo "   tokenA =$TA"
echo "   tokenB =$TB"

echo ">> creating pair"
s "$AK" "$FACTORY" "$(cast calldata 'createPair(address,address)' "$TA" "$TB")" >/dev/null
PAIR=$(q "$FACTORY" "$(cast calldata 'getPair(address,address)' "$TA" "$TB")" | cast parse-bytes32-address)
TOKEN0=$(q "$PAIR" "$(cast calldata 'token0()')" | cast parse-bytes32-address)
echo "   pair   =$PAIR (token0=$TOKEN0)"

show_state() {
  local label="$1"
  local r0 r1 ba bb
  r0=$(dec "$(q "$PAIR" 'getReserves()(uint112,uint112,uint32)' | sed -n 1p)")
  r1=$(dec "$(q "$PAIR" 'getReserves()(uint112,uint112,uint32)' | sed -n 2p)")
  ba=$(dec "$(q "$TA" 'balanceOf(address)(uint256)' "$PAIR")")
  bb=$(dec "$(q "$TB" 'balanceOf(address)(uint256)' "$PAIR")")
  echo "   [$label] reserves token0=$r0 token1=$r1 | realBal A=$ba B=$bb"
  if [[ "$TA" == "$TOKEN0" ]]; then
    [[ "$r0" == "$ba" && "$r1" == "$bb" ]] || { echo "   !! reserves != balances"; exit 1; }
  else
    [[ "$r0" == "$bb" && "$r1" == "$ba" ]] || { echo "   !! reserves != balances"; exit 1; }
  fi
}

echo ">> minting test assets"
s "$AK" "$TA" "$(cast calldata 'mint(address,uint256)' "$ALICE" $(wad 1000))" >/dev/null
s "$AK" "$TB" "$(cast calldata 'mint(address,uint256)' "$ALICE" $(wad 1000))" >/dev/null
s "$AK" "$TA" "$(cast calldata 'mint(address,uint256)' "$BOB" $(wad 1000))" >/dev/null
s "$AK" "$TB" "$(cast calldata 'mint(address,uint256)' "$BOB" $(wad 1000))" >/dev/null

echo ">> Alice bootstraps 100 A / 100 B"
s "$AK" "$TA" "$(cast calldata 'approve(address,uint256)' "$ROUTER" "$MAX_UINT")" >/dev/null
s "$AK" "$TB" "$(cast calldata 'approve(address,uint256)' "$ROUTER" "$MAX_UINT")" >/dev/null
D=$(dl)
s "$AK" "$ROUTER" "$(cast calldata 'addLiquidity(address,address,uint256,uint256,uint256,uint256,address,uint256)' \
  "$TA" "$TB" $(wad 100) $(wad 100) 0 0 "$ALICE" "$D")" >/dev/null
show_state "after bootstrap"
echo "   locked LP at address(0) = $(dec "$(q "$PAIR" 'balanceOf(address)(uint256)' 0x0000000000000000000000000000000000000000)")"

echo ">> Bob adds 50 A / 50 B proportionally"
s "$BK" "$TA" "$(cast calldata 'approve(address,uint256)' "$ROUTER" "$MAX_UINT")" >/dev/null
s "$BK" "$TB" "$(cast calldata 'approve(address,uint256)' "$ROUTER" "$MAX_UINT")" >/dev/null
s "$BK" "$ROUTER" "$(cast calldata 'addLiquidity(address,address,uint256,uint256,uint256,uint256,address,uint256)' \
  "$TA" "$TB" $(wad 50) $(wad 50) 0 0 "$BOB" "$D")" >/dev/null
show_state "after Bob deposit"

echo ">> Bob swaps exactly 10 A -> B (amountOutMin = quoted, deadline set)"
AMT_OUT=$(dec "$(q "$ROUTER" "$(cast calldata 'getAmountOut(uint256,address,address)' $(wad 10) "$TA" "$TB")")")
AMT_OUT=$(cast --to-dec "$AMT_OUT")
echo "   quoted amountOut = $AMT_OUT (30 bps fee applied)"
s "$BK" "$ROUTER" "$(cast calldata 'swapExactTokensForTokens(uint256,uint256,address[],address,uint256)' \
  $(wad 10) "$AMT_OUT" "[$TA,$TB]" "$BOB" "$D")" >/dev/null
show_state "after swap"

echo ">> Bob burns half his LP shares (pro-rata)"
BOB_LP=$(dec "$(q "$PAIR" 'balanceOf(address)(uint256)' "$BOB")")
# Large-integer half without 64-bit bash arithmetic overflow.
HALF=$(python3 -c "print($BOB_LP // 2)")
s "$BK" "$PAIR" "$(cast calldata 'approve(address,uint256)' "$ROUTER" "$MAX_UINT")" >/dev/null
s "$BK" "$ROUTER" "$(cast calldata 'removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)' \
  "$TA" "$TB" "$HALF" 0 0 "$BOB" "$D")" >/dev/null
show_state "after half withdrawal"

echo ">> Bob burns the rest; the locked 1_000 shares remain forever"
BOB_LP=$(dec "$(q "$PAIR" 'balanceOf(address)(uint256)' "$BOB")")
s "$BK" "$ROUTER" "$(cast calldata 'removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)' \
  "$TA" "$TB" "$BOB_LP" 0 0 "$BOB" "$D")" >/dev/null
show_state "after full withdrawal"
echo "   Bob circulating LP = $(dec "$(q "$PAIR" 'balanceOf(address)(uint256)' "$BOB")"), locked = $(dec "$(q "$PAIR" 'balanceOf(address)(uint256)' 0x0000000000000000000000000000000000000000)")"

echo
echo ">> demo complete: reserves matched real balances after every action"
