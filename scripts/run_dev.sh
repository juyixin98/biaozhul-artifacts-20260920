#!/usr/bin/env bash
# 本地开发启动脚本。
# 用法: ./scripts/run_dev.sh
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${SBOM_HOST:-127.0.0.1}"
PORT="${SBOM_PORT:-8000}"
exec python -m uvicorn sbom_risk.app:app --host "$HOST" --port "$PORT" --reload
