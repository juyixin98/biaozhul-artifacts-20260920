#!/usr/bin/env bash
# 运行自动化测试套件。
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests "$@"
