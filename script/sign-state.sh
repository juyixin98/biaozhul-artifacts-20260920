#!/usr/bin/env bash
#
# sign-state.sh — produce REAL secp256k1 signatures for one payment-channel
# state, two equivalent ways:
#
#   (A) Sign the contract's RAW EIP-712 digest with `cast wallet sign --no-hash`
#       (what script/live-demo.sh uses).
#   (B) Sign a standard EIP-712 TypedData JSON document with
#       `cast wallet sign --data --from-file` (interoperable; you can sign the
#       same JSON in any EIP-712 wallet).
#
# Both signatures recover, on-chain, to the payer/payee and are accepted by the
# contract because they are identical at the secp256k1 level.
#
# Usage:
#   ./script/sign-state.sh
#
# Requires a running anvil with the demo deployed. Easiest: in one terminal run
# `./script/live-demo.sh` up to step 2 (or leave an anvil running and deploy
# yourself); this script targets RPC http://127.0.0.1:8545.
set -euo pipefail
export PATH="$HOME/.foundry/bin:$PATH"
export FOUNDRY_DISABLE_NIGHTLY_WARNING=1

RPC=http://127.0.0.1:8545
PAYER_PK=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
PAYEE_PK=0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d

# Deterministic CREATE addresses on a fresh anvil (token first, adjudicator 2nd).
TOKEN=0x5FbDB2315678afecb367f032d93F642f64180aa3
ADJ=0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512
PAYER=0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
PAYEE=0x70997970C51812dc3A010C7d01b50e0d17dc79C8

# Self-contained: deploy token+adjudicator at the deterministic CREATE addresses
# if they are not already present on the target node.
if [ "$(cast code "$TOKEN" --rpc-url "$RPC")" = "0x" ]; then
  echo "deploying TestToken + PaymentAdjudicator to $RPC ..."
  forge create src/TestToken.sol:TestToken --rpc-url "$RPC" --private-key "$PAYER_PK" \
    --broadcast --json --constructor-args "Test Token" TT 18 >/dev/null
  forge create src/PaymentAdjudicator.sol:PaymentAdjudicator --rpc-url "$RPC" \
    --private-key "$PAYER_PK" --broadcast --json >/dev/null
fi

COLL=1000000000000000000000
CID=$(cast call "$ADJ" \
  "computeChannelId(address,address,address,uint256,uint64,uint64)(bytes32)" \
  "$PAYER" "$PAYEE" "$TOKEN" "$COLL" 120 1 --rpc-url "$RPC")

SEQ=1
PAID=100000000000000000000   # 100 TT
STATE="($CID,31337,$SEQ,$PAID)"

echo "channel id : $CID"
echo "state      : seq=$SEQ cumulativePaid=$PAID"

# (A) raw digest signing ------------------------------------------------------
DIGEST=$(cast call "$ADJ" \
  "stateDigest((bytes32,uint256,uint64,uint256))(bytes32)" \
  "$STATE" --rpc-url "$RPC")
echo "EIP-712 digest (A): $DIGEST"
echo "payer sig (A): $(cast wallet sign --private-key "$PAYER_PK" --no-hash "$DIGEST")"
echo "payee sig (A): $(cast wallet sign --private-key "$PAYEE_PK" --no-hash "$DIGEST")"

# (B) standard TypedData JSON signing -----------------------------------------
# Rewrite the mutable fields of the checked-in example so it matches this run.
JSON=/tmp/payment-state.json
jq --arg cid "$CID" --argjson chain 31337 --argjson seq "$SEQ" --argjson paid "$PAID" \
   --arg verifier "$ADJ" \
   '.domain.chainId=($chain|tostring) | .domain.verifyingContract=$verifier
    | .message.channelId=$cid | .message.chainId=($chain|tostring)
    | .message.sequence=$seq | .message.cumulativePaid=($paid|tostring)' \
   examples/eip712-typed-data.json > "$JSON"
echo "typed-data JSON (B) written to $JSON"
echo "payer sig (B): $(cast wallet sign --data --from-file "$JSON" --private-key "$PAYER_PK")"
echo "payee sig (B): $(cast wallet sign --data --from-file "$JSON" --private-key "$PAYEE_PK")"
echo
echo "A and B produce signatures over the SAME 32-byte digest, so both are"
echo "accepted by close()/challenge(). script/live-demo.sh proves this on-chain."
