#!/usr/bin/env bash
# 启动 HTTP 服务：./run.sh [port] [reconcilePeriodMillis]
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build ] || [ -z "$(find build -name '*.class' -print -quit)" ]; then
  ./build.sh
fi

exec java -cp build streamagg.service.Main "$@"
