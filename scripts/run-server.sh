#!/usr/bin/env bash
# 启动 JSON HTTP 服务：scripts/run-server.sh [port]，默认 8080。
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8080}"
java -cp "$ROOT_DIR/build/classes" com.tjoin.service.Main "$PORT"
