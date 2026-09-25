#!/usr/bin/env bash
# 启动 JSON 服务（默认 127.0.0.1:8080，仅本机访问）。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d build/classes ]; then
  ./scripts/build.sh
fi

exec java -cp build/classes com.example.segmenter.Main "$@"
