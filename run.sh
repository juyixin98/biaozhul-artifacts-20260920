#!/usr/bin/env bash
# 启动服务：./run.sh [端口] [CSV 文件路径]
#   ./run.sh                 端口 8080，不自动加载（启动后 POST /load）
#   ./run.sh 9090 data/sample.csv
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p out
javac -encoding UTF-8 -d out $(find src -name '*.java')

PORT="${1:-8080}"
if [[ $# -ge 2 ]]; then
  exec java -cp out bitserver.Main "$PORT" --autoload "$2"
else
  exec java -cp out bitserver.Main "$PORT"
fi
