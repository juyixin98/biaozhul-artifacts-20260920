#!/usr/bin/env bash
# 运行全部自动化测试。
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v
