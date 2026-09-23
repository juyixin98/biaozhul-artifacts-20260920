#!/usr/bin/env bash
# 编译并启动服务（默认端口 8080，可用 PORT 环境变量或第一个参数覆盖）
set -euo pipefail
cd "$(dirname "$0")/.."

./scripts/build.sh
java -cp out com.bm25stable.Main "$@"
