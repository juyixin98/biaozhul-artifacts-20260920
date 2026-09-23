#!/usr/bin/env bash
# 启动 JSON HTTP 服务：./run-server.sh [port]
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8080}"
exec java -cp target/classes com.example.quantiles.service.Main serve --port="$PORT"
