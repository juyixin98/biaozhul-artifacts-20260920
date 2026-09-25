#!/usr/bin/env bash
# 启动 JSON 服务：scripts/run-server.sh [port] [dictDir]
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d build/classes ] || scripts/build.sh
PORT="${1:-8080}"
DICT="${2:-data/dicts}"
exec java -Dfile.encoding=UTF-8 -cp build/classes com.example.seg.service.SegHttpServer "$PORT" "$DICT"
