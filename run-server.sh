#!/usr/bin/env bash
# 启动 JSON HTTP 服务（需先执行 ./build.sh）。
# 环境变量：PORT（默认 8080）、BIND（默认 127.0.0.1）、MODE（EVENT_TIME/PROCESSING_TIME）、
#           WINDOW_MILLIS、ALLOWED_LATENESS_MILLIS、MATCH_POLICY、LATE_POLICY
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build/classes ]; then
  ./build.sh
fi
exec java -cp build/classes streammatch.service.ApiServer
