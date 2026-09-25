#!/usr/bin/env bash
# 启动 JSON/HTTP 服务。用法: ./run.sh [port]
set -euo pipefail
cd "$(dirname "$0")"

CLASSES=build/classes
if [ ! -d "$CLASSES" ]; then
  ./build.sh compile
fi
PORT="${1:-${PORT:-8080}}"
exec java -cp "$CLASSES" cep.service.Main "$PORT"
