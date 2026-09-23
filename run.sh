#!/usr/bin/env bash
# 启动服务：./run.sh [port]
set -euo pipefail
cd "$(dirname "$0")"
JAVA="${JAVA:-java}"
if [ ! -d build/classes ]; then ./build.sh; fi
exec "$JAVA" -cp build/classes incagg.web.Main "${1:-8080}"
