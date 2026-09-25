#!/usr/bin/env bash
# 自动化测试入口：unittest 全量发现。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m unittest discover -s tests -v
