#!/usr/bin/env bash
# 一键运行全部自动化测试（从项目根目录、以包方式发现测试）。
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -t . -s tests -v
