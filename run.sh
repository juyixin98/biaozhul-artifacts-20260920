#!/usr/bin/env bash
# 启动服务：./run.sh [端口] [数据目录]
set -euo pipefail
cd "$(dirname "$0")"
[ -d out ] || ./build.sh
PORT="${1:-${CDC_PORT:-8080}}"
DATA="${2:-${CDC_DATA_DIR:-data}}"
exec java -cp out com.example.cdc.Main "$PORT" "$DATA"
