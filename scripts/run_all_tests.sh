#!/usr/bin/env bash
# Full local verification: build contracts, run Foundry tests, then boot Anvil
# and run the Python integration + API tests. All networking is loopback only.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$PATH:$HOME/.foundry/bin"

echo "==> [1/3] forge build"
forge build

echo "==> [2/3] forge test (in-process EVM unit + fuzz tests)"
forge test -vv

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> [3/3] pytest (boots its own Anvil on 127.0.0.1:8546)"
pytest

echo "All checks passed."
