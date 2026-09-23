#!/usr/bin/env bash
# accept.sh —— 一键验收：编译 + 全部测试 + 双链真实回放。任一失败即非零退出。
set -euo pipefail
export PATH="$PATH:${HOME}/.foundry/bin"
cd "$(dirname "${BASH_SOURCE[0]}")"

echo "=========== [1/3] forge build ==========="
forge build

echo "=========== [2/3] forge test ==========="
forge test -vv

echo "=========== [3/3] dual-chain replay ==========="
./script/replay.sh

echo
echo "✅ 验收全部通过"
