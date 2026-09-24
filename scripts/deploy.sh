#!/usr/bin/env bash
# Build contracts, start Anvil (if needed), deploy SyntheticToken +
# LinearTokenVesting, and write deploy/addresses.json for the Python API.
#
# Usage:
#   ./scripts/deploy.sh                 # build + deploy to http://127.0.0.1:8545
#   START_ANVIL=1 ./scripts/deploy.sh   # also launch a local anvil node
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Allow an isolated Foundry install (shared machines may collide on ~/.foundry).
FOUNDRY_DIR="${FOUNDRY_DIR:-$HOME/.foundry}"
export PATH="$FOUNDRY_DIR/bin:$PATH"
RPC_URL="${RPC_URL:-http://127.0.0.1:8545}"
RPC_HOST_PORT="${RPC_URL#http://}"
INITIAL_SUPPLY="${INITIAL_SUPPLY:-1000000000000000000000000}"  # 1,000,000 tokens

if [ "${START_ANVIL:-0}" = "1" ]; then
  if ! curl -sf -X POST -H 'Content-Type: application/json' \
      --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
      "$RPC_URL" >/dev/null 2>&1; then
    echo "Starting anvil on $RPC_URL ..."
    nohup anvil --host 127.0.0.1 --port "${RPC_HOST_PORT##*:}" --chain-id 31337 \
      > "$ROOT/deploy/anvil.log" 2>&1 &
    echo $! > "$ROOT/deploy/anvil.pid"
    for _ in $(seq 1 30); do
      curl -sf -X POST -H 'Content-Type: application/json' \
        --data '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' \
        "$RPC_URL" >/dev/null 2>&1 && break
      sleep 0.5
    done
  fi
fi

echo "Building contracts..."
forge build

echo "Deploying to $RPC_URL ..."
# Anvil account #0 — test key only.
PRIVATE_KEY="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

TOKEN_OUT=$(forge create --broadcast --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY" \
  src/SyntheticToken.sol:SyntheticToken --constructor-args "$INITIAL_SUPPLY")
TOKEN_ADDR=$(echo "$TOKEN_OUT" | grep 'Deployed to:' | awk '{print $3}')
echo "SyntheticToken: $TOKEN_ADDR"

VEST_OUT=$(forge create --broadcast --rpc-url "$RPC_URL" --private-key "$PRIVATE_KEY" \
  src/LinearTokenVesting.sol:LinearTokenVesting --constructor-args "$TOKEN_ADDR")
VEST_ADDR=$(echo "$VEST_OUT" | grep 'Deployed to:' | awk '{print $3}')
echo "LinearTokenVesting: $VEST_ADDR"

mkdir -p "$ROOT/deploy"
cat > "$ROOT/deploy/addresses.json" <<EOF
{
  "token": "$TOKEN_ADDR",
  "vesting": "$VEST_ADDR",
  "rpc_url": "$RPC_URL",
  "chain_id": 31337,
  "deployer": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
}
EOF
echo "Wrote deploy/addresses.json"
cat "$ROOT/deploy/addresses.json"
