#!/usr/bin/env bash
# 一键运行全部自动化测试（标准库 unittest，无需第三方测试框架）。
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v
