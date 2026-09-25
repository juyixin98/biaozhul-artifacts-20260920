#!/usr/bin/env bash
# 一键运行全部自动化测试（标准库 unittest，无第三方依赖）。
set -euo pipefail

cd "$(dirname "$0")"

# 生成 examples/ 请求样例（CLI 测试也会用到 01 号样例）
python3 scripts/make_examples.py

# -v 详细输出；discover 自动发现 tests/test_*.py
exec python3 -m unittest discover -s tests -v
