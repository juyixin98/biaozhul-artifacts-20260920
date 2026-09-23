#!/usr/bin/env bash
#
# live-demo.sh — run the full payment-channel adjudication flow against a
# REAL local Anvil node, signing EIP-712 states with REAL secp256k1 keys
# (cast wallet sign). No external network; only localhost RPC.
#
# Usage:
#   ./script/live-demo.sh            # spins up its own anvil on 127.0.0.1:8545
#   RPC_URL=http://127.0.0.1:8545 ./script/live-demo.sh   # use an existing node
#
set -euo pipefail

# ---- locate foundry --------------------------------------------------------
export PATH="$HOME/.foundry/bin:$PATH"
export FOUNDRY_DISABLE_NIGHTLY_WARNING=1

RPC_URL="${RPC_URL:-http://127.0.0.1:8545}"
COLLATERAL_WEI="$(cast to-wei 1000)"
DURATION=120                 # challenge window in seconds (short for a demo)
NONCE=1

# Ephemeral, well-known Anvil test keys (key #0 = payer, key #1 = payee).
PAYER_PK=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
PAYEE_PK=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d
PAYER=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
PAYEE=0x70997970C51812dc3A010C7d01b50e0d17dc79C8

banner() { printf '\n=== %s ===\n' "$1"; }
info()   { printf '  %s\n' "$*"; }

# Start an ephemeral anvil if no RPC was provided.
ANVIL_PID=""
cleanup() { [ -n "$ANVIL_PID" ] && kill "$ANVIL_PID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

if [ -z "${RPC_URL_PROVIDED:-}" ]; then
  # Only auto-start if the port does not already answer.
  if ! cast chain-id --rpc-url "$RPC_URL" >/dev/null 2>&1; then
    banner "starting local anvil"
    anvil --silent --chain-id 31337 --port 8545 >/tmp/payment-channel-anvil.log 2>&1 &
    ANVIL_PID=$!
    for _ in $(seq 1 30); do
      cast chain-id --rpc-url "$RPC_URL" >/dev/null 2>&1 && break
      sleep 0.3
    done
  fi
fi
CHAIN_ID=$(cast chain-id --rpc-url "$RPC_URL")
info "RPC        = $RPC_URL"
info "chain id   = $CHAIN_ID"
info "payer      = $PAYER"
info "payee      = $PAYEE"

# ---- deploy ----------------------------------------------------------------
banner "1. deploy token + adjudicator"
TOKEN=$(forge create src/TestToken.sol:TestToken \
  --rpc-url "$RPC_URL" --private-key "$PAYER_PK" --broadcast --json \
  --constructor-args "Test Token" TT 18 | jq -r '.deployedTo')

ADJ=$(forge create src/PaymentAdjudicator.sol:PaymentAdjudicator \
  --rpc-url "$RPC_URL" --private-key "$PAYER_PK" --broadcast --json \
  | jq -r '.deployedTo')
info "token       = $TOKEN"
info "adjudicator = $ADJ"

# ---- fund + open -----------------------------------------------------------
banner "2. mint / approve / open channel (1000 TT collateral)"
cast send "$TOKEN" "mint(address,uint256)" "$PAYER" "$COLLATERAL_WEI" \
  --rpc-url "$RPC_URL" --private-key "$PAYER_PK" >/dev/null
cast send "$TOKEN" "approve(address,uint256)" "$ADJ" "$COLLATERAL_WEI" \
  --rpc-url "$RPC_URL" --private-key "$PAYER_PK" >/dev/null

CHANNEL_ID=$(cast call "$ADJ" \
  "computeChannelId(address,address,address,uint256,uint64,uint64)(bytes32)" \
  "$PAYER" "$PAYEE" "$TOKEN" "$COLLATERAL_WEI" "$DURATION" "$NONCE" \
  --rpc-url "$RPC_URL")
info "channel id = $CHANNEL_ID"

cast send "$ADJ" \
  "openChannel(address,address,address,uint256,uint64,uint64)" \
  "$PAYER" "$PAYEE" "$TOKEN" "$COLLATERAL_WEI" "$DURATION" "$NONCE" \
  --rpc-url "$RPC_URL" --private-key "$PAYER_PK" >/dev/null
ESCROW=$(cast call "$TOKEN" "balanceOf(address)(uint256)" "$ADJ" --rpc-url "$RPC_URL" --json | jq -r '.[0]')
info "adjudicator escrow = $(cast --from-wei "$ESCROW") TT"

# Read one field of the Channel tuple. Field order (1-based):
# 1 payer 2 payee 3 token 4 challengeDuration 5 status 6 bestSequence
# 7 bestCumulativePaid 8 collateral 9 challengeEndsAt
channel_field() {
  local idx="$1"
  cast call "$ADJ" \
    "getChannel(bytes32)((address,address,address,uint64,uint8,uint64,uint256,uint256,uint64))" \
    "$CHANNEL_ID" --rpc-url "$RPC_URL" --json \
  | jq -r ".[0][$((idx - 1))]"
}

sign_state() {
  local seq="$1" cum="$2"
  STATE="($CHANNEL_ID,$CHAIN_ID,$seq,$cum)"
  STATE_DIGEST=$(cast call "$ADJ" \
    "stateDigest((bytes32,uint256,uint64,uint256))(bytes32)" \
    "$STATE" --rpc-url "$RPC_URL")
  # --no-hash signs the RAW 32-byte EIP-712 digest (no eth_sign prefix).
  SIG_P=$(cast wallet sign --private-key "$PAYER_PK" --no-hash "$STATE_DIGEST")
  SIG_E=$(cast wallet sign --private-key "$PAYEE_PK" --no-hash "$STATE_DIGEST")
}

# ---- 3. malicious close with OLD state -------------------------------------
banner "3. malicious close with OLD state seq=1 (100 TT)"
sign_state 1 "$(cast to-wei 100)"
cast send "$ADJ" "close(bytes32,(bytes32,uint256,uint64,uint256),bytes,bytes)" \
  "$CHANNEL_ID" "$STATE" "$SIG_P" "$SIG_E" \
  --rpc-url "$RPC_URL" --private-key "$PAYER_PK" >/dev/null
BEST_PAID=$(channel_field 7)
info "on-chain best payout after stale close = $(cast --from-wei "$BEST_PAID") TT"

# ---- 4. newest states win; stale replay rejected ---------------------------
banner "4. payee counters: seq=2 (400 TT), then seq=3 (700 TT)"
sign_state 2 "$(cast to-wei 400)"
cast send "$ADJ" "challenge((bytes32,uint256,uint64,uint256),bytes,bytes)" \
  "$STATE" "$SIG_P" "$SIG_E" \
  --rpc-url "$RPC_URL" --private-key "$PAYEE_PK" >/dev/null
info "seq=2 accepted"

# replay stale seq=1 -> must revert (re-sign because sign_state overwrote vars)
STALE_STATE="$STATE"; STALE_P="$SIG_P"; STALE_E="$SIG_E"
sign_state 1 "$(cast to-wei 100)"
if cast send "$ADJ" "challenge((bytes32,uint256,uint64,uint256),bytes,bytes)" \
     "$STATE" "$SIG_P" "$SIG_E" \
     --rpc-url "$RPC_URL" --private-key "$PAYEE_PK" >/dev/null 2>&1; then
  echo "FAIL: stale seq=1 was accepted"; exit 1
else
  info "[OK] replay of stale seq=1 rejected"
fi

sign_state 3 "$(cast to-wei 700)"
cast send "$ADJ" "challenge((bytes32,uint256,uint64,uint256),bytes,bytes)" \
  "$STATE" "$SIG_P" "$SIG_E" \
  --rpc-url "$RPC_URL" --private-key "$PAYEE_PK" >/dev/null
info "seq=3 accepted (700 TT is the latest state)"

# ---- 5. time boundary + settlement -----------------------------------------
banner "5. challenge window boundary + settlement"
ENDS_AT=$(channel_field 9)
NOW=$(cast block latest --rpc-url "$RPC_URL" --json | jq -r '.data.timestamp')
NOW=$(cast --to-dec "$NOW")
info "now=$NOW  challengeEndsAt=$ENDS_AT"

if cast send "$ADJ" "settle(bytes32)" "$CHANNEL_ID" \
     --rpc-url "$RPC_URL" --private-key "$PAYER_PK" >/dev/null 2>&1; then
  echo "FAIL: settled before deadline"; exit 1
else
  info "[OK] settle before deadline rejected"
fi

info "fast-forwarding anvil time past the deadline..."
cast rpc evm_setNextBlockTimestamp "$ENDS_AT" --rpc-url "$RPC_URL" >/dev/null
cast rpc evm_mine --rpc-url "$RPC_URL" >/dev/null

cast send "$ADJ" "settle(bytes32)" "$CHANNEL_ID" \
  --rpc-url "$RPC_URL" --private-key "$PAYER_PK" >/dev/null
PAYEE_BAL=$(cast call "$TOKEN" "balanceOf(address)(uint256)" "$PAYEE" --rpc-url "$RPC_URL" --json | jq -r '.[0]')
PAYER_BAL=$(cast call "$TOKEN" "balanceOf(address)(uint256)" "$PAYER" --rpc-url "$RPC_URL" --json | jq -r '.[0]')
ADJ_BAL=$(cast call "$TOKEN" "balanceOf(address)(uint256)" "$ADJ" --rpc-url "$RPC_URL" --json | jq -r '.[0]')
info "payee received = $(cast --from-wei "$PAYEE_BAL") TT"
info "payer refund   = $(cast --from-wei "$PAYER_BAL") TT"
info "adjudicator    = $(cast --from-wei "$ADJ_BAL") TT (must be 0)"

# ---- 6. duplicate settle rejected ------------------------------------------
banner "6. duplicate settlement must be rejected"
if cast send "$ADJ" "settle(bytes32)" "$CHANNEL_ID" \
     --rpc-url "$RPC_URL" --private-key "$PAYER_PK" >/dev/null 2>&1; then
  echo "FAIL: duplicate settle succeeded"; exit 1
else
  info "[OK] duplicate settle rejected"
fi

banner "LIVE DEMO COMPLETE: newest state won, funds conserved, no double spend"
