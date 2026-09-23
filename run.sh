#!/usr/bin/env bash
# 启动区间重叠索引 HTTP 服务。
# 用法：./run.sh [port] [bind-host]
#   PORT=8080 ./run.sh          通过环境变量指定端口
#   BIND_HOST=0.0.0.0 ./run.sh  允许外部访问（默认仅 127.0.0.1）
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build/classes ]; then
  ./build.sh
fi

JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"
if ! command -v "$JAVA_BIN" >/dev/null 2>&1; then
  echo "错误：找不到 java。请安装 JDK 17+ 或设置 JAVA_HOME。" >&2
  exit 1
fi

exec "$JAVA_BIN" -cp build/classes intervalindex.HttpServerApp "$@"
