#!/usr/bin/env bash
# 启动 JSON 服务：./run.sh [port]  (默认 8080)
set -euo pipefail
cd "$(dirname "$0")"
./build.sh
java -cp build/classes com.example.edcand.App serve "${1:-8080}"
