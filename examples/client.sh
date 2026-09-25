#!/usr/bin/env bash
# 用系统自带的 nc/netcat 向 reasm-server 发送 NDJSON 请求样例。
#
# 用法：
#   ./examples/client.sh [port]
# 默认端口 9000（与服务端默认一致）。
#
# 前置：先在另一个终端启动服务
#   cargo run -- --port 9000
set -euo pipefail

PORT="${1:-9000}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v nc >/dev/null 2>&1; then
  echo "error: 需要 nc(netcat)；或用 python3 版本" >&2
  exit 1
fi

echo "# sending ${HERE}/requests.ndjson to 127.0.0.1:${PORT}"
# -N：发完输入后半关闭写端（服务端是长连接，不会主动断开）
nc -N "127.0.0.1" "${PORT}" < "${HERE}/requests.ndjson"
