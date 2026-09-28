#!/usr/bin/env bash
# 一键复现：运行全部测试并输出覆盖率；日志写入 logs/ 并带运行身份。
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Python =="
python3 --version

echo "== 依赖检查 =="
python3 - <<'PY'
import fastapi, uvicorn, pydantic, httpx, pytest
print("fastapi", fastapi.__version__, "| pytest", pytest.__version__)
PY

echo "== 运行测试（含覆盖率）=="
python3 -m pytest --cov=app --cov-report=term-missing "$@"
