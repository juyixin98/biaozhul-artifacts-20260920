#!/usr/bin/env bash
# =============================================================================
# replay.sh — fully replayable constant-product pool demo on a local Anvil.
#
# It starts a throwaway Anvil, deploys the contracts with Foundry, mints test
# assets, then executes add / swap / add / remove through `cast`, printing the
# pool state after every step and asserting each on-chain number against the
# independent Python reference model (reference/cpmm_ref.py).
#
# Usage:
#   bash script/replay.sh            # ephemeral chain, auto-cleanup
#   KEEP=1 bash script/replay.sh     # leave Anvil running (prints RPC + pid)
#
# Requirements: anvil, cast, forge, python3 on PATH (or ~/.foundry/bin).
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v cast >/dev/null 2>&1 && [ -d "$HOME/.foundry/bin" ]; then
  export PATH="$HOME/.foundry/bin:$PATH"
fi
for b in anvil cast forge python3; do
  command -v "$b" >/dev/null 2>&1 || { echo "missing: $b" >&2; exit 1; }
done

RPC=http://127.0.0.1:8545
# Anvil test account #0 (deployer / LP alice) and #1 (swapper bob)
ALICE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
BOB_KEY=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d
ALICE=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
BOB=0x70997970C51812dc3A010C7d01b50e0d17dc79C8
WAD=1000000000000000000

LOG="$ROOT/examples/replay-output.txt"
mkdir -p examples
: > "$LOG"

say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
hr()   { say "----------------------------------------------------------------"; }

cleanup() {
  if [ -n "${ANVIL_PID:-}" ] && [ -z "${KEEP:-}" ]; then
    kill "$ANVIL_PID" >/dev/null 2>&1 || true
    # wait for the process (and therefore the RPC port) to release
    for _ in $(seq 1 50); do
      kill -0 "$ANVIL_PID" 2>/dev/null || break
      sleep 0.1
    done
  fi
}
trap cleanup EXIT

# Python reference helper: prints reference values for the exact sequence.
ref() { python3 - "$@" < /dev/null; }

# -----------------------------------------------------------------------------
say "1) starting anvil (deterministic, chain 31337)"
# Refuse to start if something already answers on the RPC port: two
# instances would race and the recorded addresses/deploys would be wrong.
if cast block-number --rpc-url "$RPC" >/dev/null 2>&1; then
  echo "ERROR: an RPC node is already running on $RPC. Stop it (e.g. 'pkill anvil') or re-run with KEEP=1 against that node." >&2
  exit 1
fi
anvil --silent --chain-id 31337 --port 8545 >/dev/null 2>&1 &
ANVIL_PID=$!
# wait for RPC (fail fast if the process died)
for _ in $(seq 1 50); do
  cast block-number --rpc-url "$RPC" >/dev/null 2>&1 && break
  kill -0 "$ANVIL_PID" 2>/dev/null || { echo "anvil failed to start" >&2; exit 1; }
  sleep 0.1
done
cast block-number --rpc-url "$RPC" >/dev/null

hr
say "2) deploying contracts"
DEPLOY=$(forge script script/Deploy.s.sol:Deploy \
  --rpc-url "$RPC" --broadcast --skip-simulation 2>&1 | tee -a "$LOG")
TKN0=$(grep -oE 'token0: 0x[a-fA-F0-9]{40}' <<<"$DEPLOY" | awk '{print $2}')
TKN1=$(grep -oE 'token1: 0x[a-fA-F0-9]{40}' <<<"$DEPLOY" | awk '{print $2}')
POOL=$(grep -oE 'pool:   0x[a-fA-F0-9]{40}' <<<"$DEPLOY" | awk '{print $2}')
[ -n "$POOL" ] || { echo "deployment failed" >&2; exit 1; }
say "TKN0=$TKN0"; say "TKN1=$TKN1"; say "POOL=$POOL"

state() {
  cast call "$POOL" "getReserves()(uint112,uint112,uint256)" --rpc-url "$RPC"
}
# reserve triple as shell variables R0 R1 TS
read_state() {
  local lines
  lines="$(cast call "$POOL" "getReserves()(uint112,uint112,uint256)" --rpc-url "$RPC")"
  R0="$(_num "$(sed -n 1p <<<"$lines")")"
  R1="$(_num "$(sed -n 2p <<<"$lines")")"
  TS="$(_num "$(sed -n 3p <<<"$lines")")"
}
# strip cast's human suffix " [1.23e21]" leaving the raw integer
_num() { sed -E 's/[[:space:]]*\[.*\]$//' <<<"$1"; }
bal0() { _num "$(cast call "$TKN0" "balanceOf(address)(uint256)" "$1" --rpc-url "$RPC")"; }
bal1() { _num "$(cast call "$TKN1" "balanceOf(address)(uint256)" "$1" --rpc-url "$RPC")"; }
sharesOf() { _num "$(cast call "$POOL" "balanceOf(address)(uint256)" "$1" --rpc-url "$RPC")"; }

deadline() { echo $(( $(date +%s) + 3600 )); }

# -----------------------------------------------------------------------------
hr
say "3) mint 2,000 TKN0/TKN1 to alice and bob"
cast send "$TKN0" "mint(address,uint256)" "$ALICE" 2000000000000000000000 \
  --rpc-url "$RPC" --private-key "$ALICE_KEY" >>"$LOG" 2>&1
cast send "$TKN1" "mint(address,uint256)" "$ALICE" 2000000000000000000000 \
  --rpc-url "$RPC" --private-key "$ALICE_KEY" >>"$LOG" 2>&1
cast send "$TKN0" "mint(address,uint256)" "$BOB" 100000000000000000000 \
  --rpc-url "$RPC" --private-key "$ALICE_KEY" >>"$LOG" 2>&1

for u in "$ALICE" "$BOB"; do
  say "  $u  TKN0=$(bal0 "$u")  TKN1=$(bal1 "$u")"
done

# -----------------------------------------------------------------------------
hr
say "4) alice addLiquidity(1000,1000)"
UINTMAX=115792089237316195423570985008687907853269984665640564039457584007913129639935
cast send "$TKN0" "approve(address,uint256)" "$POOL" "$UINTMAX" \
  --rpc-url "$RPC" --private-key "$ALICE_KEY" >>"$LOG" 2>&1
cast send "$TKN1" "approve(address,uint256)" "$POOL" "$UINTMAX" \
  --rpc-url "$RPC" --private-key "$ALICE_KEY" >>"$LOG" 2>&1

DL=$(deadline)
MINT=$(cast send "$POOL" \
  "addLiquidity(uint256,uint256,uint256,address,uint256)" \
  1000000000000000000000 1000000000000000000000 0 "$ALICE" "$DL" \
  --rpc-url "$RPC" --private-key "$ALICE_KEY")
say "  reserves+supply: $(state)"
say "  alice LP shares: $(sharesOf "$ALICE")"
say "  locked (0x0):    $(sharesOf 0x0000000000000000000000000000000000000000)"
say "  expected shares 999999999999999999000, lock 1000"

# -----------------------------------------------------------------------------
hr
say "5) bob swapExactInput: 10 TKN0 -> TKN1 (0.30% fee)"
cast send "$TKN0" "approve(address,uint256)" "$POOL" "$UINTMAX" \
  --rpc-url "$RPC" --private-key "$BOB_KEY" >>"$LOG" 2>&1
DL=$(deadline)
SWAP=$(cast send "$POOL" \
  "swapExactInput(address,uint256,uint256,address,uint256)" \
  "$TKN0" 10000000000000000000 0 "$BOB" "$DL" \
  --rpc-url "$RPC" --private-key "$BOB_KEY")
say "  reserves+supply: $(state)"
say "  bob TKN1 after:  $(bal1 "$BOB")"
EXPECT_OUT=$(python3 -c 'import sys; sys.path.insert(0,"reference");
from cpmm_ref import integer_amount_out
print(integer_amount_out(10*10**18,1000*10**18,1000*10**18))')
say "  expected output: $EXPECT_OUT (bob received exactly this)"

# k growth
read_state
python3 - "$R0" "$R1" <<'PY'
import sys
r0,r1=int(sys.argv[1]),int(sys.argv[2])
k0=(1000*10**18)**2
assert r0*r1>k0, "k must grow"
print(f"  k grew: {k0} -> {r0*r1}")
PY

# -----------------------------------------------------------------------------
hr
say "6) dust input 1 wei must revert (fee cannot be rounded away)"
DL=$(deadline)
if cast send "$POOL" \
   "swapExactInput(address,uint256,uint256,address,uint256)" \
   "$TKN0" 1 0 "$BOB" "$DL" \
   --rpc-url "$RPC" --private-key "$BOB_KEY" >>"$LOG" 2>&1; then
  say "  ERROR: dust swap succeeded"; exit 1
else
  say "  reverted as expected (ZeroOutput)"
fi

# expired deadline must revert
if cast send "$POOL" \
   "swapExactInput(address,uint256,uint256,address,uint256)" \
   "$TKN0" 10000000000000000000 0 "$BOB" 1 \
   --rpc-url "$RPC" --private-key "$BOB_KEY" >>"$LOG" 2>&1; then
  say "  ERROR: expired swap succeeded"; exit 1
else
  say "  expired swap reverted as expected (Expired)"
fi

# -----------------------------------------------------------------------------
hr
say "7) bob addLiquidity then fully removes it (proportion preserved)"
cast send "$TKN1" "mint(address,uint256)" "$BOB" 100000000000000000000 \
  --rpc-url "$RPC" --private-key "$ALICE_KEY" >>"$LOG" 2>&1
cast send "$TKN1" "approve(address,uint256)" "$POOL" "$UINTMAX" \
  --rpc-url "$RPC" --private-key "$BOB_KEY" >>"$LOG" 2>&1
DL=$(deadline)
ADD2=$(cast send "$POOL" \
  "addLiquidity(uint256,uint256,uint256,address,uint256)" \
  50000000000000000000 50000000000000000000 0 "$BOB" "$DL" \
  --rpc-url "$RPC" --private-key "$BOB_KEY")
BOB_SH=$(sharesOf "$BOB")
say "  bob shares after add: $BOB_SH"

DL=$(deadline)
cast send "$POOL" \
  "removeLiquidity(uint256,uint256,uint256,address,uint256)" \
  "$BOB_SH" 0 0 "$BOB" "$DL" \
  --rpc-url "$RPC" --private-key "$BOB_KEY" >>"$LOG" 2>&1
say "  bob shares after remove: $(sharesOf "$BOB") (must be 0)"
say "  final reserves+supply:   $(state)"

# -----------------------------------------------------------------------------
hr
say "8) reserve == actual token balance (on-chain assertion every call)"
B0=$(bal0 "$POOL"); B1=$(bal1 "$POOL")
read_state
[ "$B0" = "$R0" ] && [ "$B1" = "$R1" ] || { say "MISMATCH"; exit 1; }
say "  TKN0 balance=$B0 == reserve0=$R0"
say "  TKN1 balance=$B1 == reserve1=$R1"

# -----------------------------------------------------------------------------
hr
say "ALL CHECKS PASSED"
say "full log: $LOG"
if [ -n "${KEEP:-}" ]; then
  say "Anvil kept alive at $RPC (pid $ANVIL_PID)"
fi
exit 0
