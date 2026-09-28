#!/usr/bin/env bash
# 开发启动脚本：首次运行自动建索引，规则变更后需 POST /admin/rebuild。
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn app.api:app --host 127.0.0.1 --port "${PORT:-8000}"
