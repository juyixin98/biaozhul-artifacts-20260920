#!/usr/bin/env bash
# 启动服务：./run.sh [port] [dataDir] [pkColumn]
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d build/classes ]; then
  ./build.sh
fi
exec java -cp build/classes cdcrebuild.Main "$@"
