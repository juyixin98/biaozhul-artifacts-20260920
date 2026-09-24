#!/usr/bin/env bash
# 一键运行全部测试：Forge 合约单元测试 + pytest（自动拉起独立 Anvil）
set -euo pipefail

# 确保找到 ~/.foundry/bin（anvil/forge）
export PATH="$HOME/.foundry/bin:$PATH"

cd "$(dirname "$0")"

echo "=================== 1/3 forge build ==================="
forge build

echo "=================== 2/3 forge test ==================="
forge test -vv

echo "=================== 3/3 pytest (自带 Anvil) ==================="
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements-lock.txt
fi
.venv/bin/python -m pytest tests/

echo
echo "ALL TESTS PASSED"
