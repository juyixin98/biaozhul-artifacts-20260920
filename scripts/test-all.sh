#!/usr/bin/env bash
# 一键跑全部自动化测试：forge（合约层）+ pytest（两条 anvil 链上端到端）。
# pytest 会自管两条测试用 anvil（端口 18645/18646），与手动启动的演示链互不干扰。
set -euo pipefail
cd "$(dirname "$0")/.."

export PATH="${FOUNDRY_BIN:-$HOME/.foundry/bin}:$PATH"

echo "==> forge build"
forge build

echo "==> forge test（合约状态机：截止时刻/互斥/重入/错误原像…）"
forge test -vv

echo "==> pytest（两条本地 anvil 链上的端到端 + HTTP 接口 + 协调器）"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.lock
fi
.venv/bin/python -m pytest tests/ -v
