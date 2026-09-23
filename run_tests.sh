#!/usr/bin/env bash
# 运行全部自动化测试（标准库 unittest，无需第三方依赖）。
set -eu
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -p 'test_*.py' -v
