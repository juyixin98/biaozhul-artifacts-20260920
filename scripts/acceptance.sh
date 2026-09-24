#!/usr/bin/env bash
# 一键验收：创建隔离虚拟环境（如不存在）、安装锁定依赖、生成示例、运行全部自动化测试。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip -q
python -m pip install -q -r requirements.lock

python scripts/generate_examples.py
python -m pytest "$@"
