#!/usr/bin/env bash
# Run the full automated test suite: Foundry unit tests + Python/web3.py tests.
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="$HOME/.foundry/bin:$PATH"

echo "== forge build =="
forge build

echo "== forge test =="
forge test -v

echo "== pytest =="
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt
python -m pytest tests/

echo "ALL TESTS PASSED"
