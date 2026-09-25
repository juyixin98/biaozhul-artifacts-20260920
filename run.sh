#!/usr/bin/env bash
# 构建并启动 HTTP 服务。可通过环境变量覆盖：
#   BM25_PORT=9090 BM25_RETAIN_SNAPSHOTS=3 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

./build.sh

OUT=build/classes
exec java -cp "$OUT" com.bm25pager.Main
