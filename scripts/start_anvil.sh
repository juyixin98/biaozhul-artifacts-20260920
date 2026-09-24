#!/usr/bin/env bash
# Start a local Anvil node with deterministic test accounts.
# An RPC server listens on 127.0.0.1:8545.
set -euo pipefail

export PATH="$HOME/.foundry/bin:$PATH"

PORT="${ANVIL_PORT:-8545}"

exec anvil \
  --host 127.0.0.1 \
  --port "$PORT" \
  --chain-id 31337 \
  --block-time 1 \
  --accounts 10 \
  --balance 10000
