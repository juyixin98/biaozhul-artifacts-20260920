#!/usr/bin/env bash
# 一键验收：运行全部自动化测试（含随机差分测试与 HTTP 端到端测试）。
# 用法：bash run_tests.sh
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v
