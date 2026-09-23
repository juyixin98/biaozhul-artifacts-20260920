#!/usr/bin/env bash
# 启动 JSON HTTP 服务: ./serve.sh [port]
# 预加载 examples/table_orders.json，因此请求里可以直接写 "table":"orders"。
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8080}"
java -cp target/classes vecq.Main serve --port "$PORT" examples/table_orders.json
