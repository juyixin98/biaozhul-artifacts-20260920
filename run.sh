#!/usr/bin/env bash
# 启动 JSON 去重服务。用法: ./run.sh [端口]
set -euo pipefail
cd "$(dirname "$0")"
[ -f build/dedup.jar ] || ./build.sh >/dev/null
PORT="${1:-8080}"
exec java -jar build/dedup.jar --port "$PORT" \
  --allowed-lateness-ms 5000 \
  --max-tombstones 100000 \
  --watermark-mode manual \
  --state-file state/snapshot.json
