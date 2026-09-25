#!/usr/bin/env bash
# 启动短语位置检索 JSON 服务。
# 用法: scripts/run_server.sh [--port 8080] [--analyzer standard|stop_gap] [--corpus data/xxx.json]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -d build/classes ]; then
  bash scripts/build.sh
fi

exec java -cp build/classes com.example.phrasesearch.Main "$@"
