#!/usr/bin/env bash
# 启动 ResFlow JSON 服务。用法: bash serve.sh [port]
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8080}"
python3 -m resflow.cli serve --host 127.0.0.1 --port "$PORT"
