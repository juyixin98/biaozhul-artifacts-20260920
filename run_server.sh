#!/usr/bin/env bash
# 编译并启动服务。用法: ./run_server.sh [port] [windowMs]
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v javac >/dev/null 2>&1; then
  if [ -x "$HOME/jdk/bin/javac" ]; then
    export PATH="$HOME/jdk/bin:$PATH"
  else
    echo "ERROR: 未找到 javac，请安装 JDK 17+ 或把 JDK 解压到 ~/jdk" >&2
    exit 1
  fi
fi

mkdir -p build/classes
javac -encoding UTF-8 -d build/classes $(find src -name '*.java')

PORT="${1:-${PORT:-8080}}"
WINDOW_MS="${2:-${WINDOW_MS:-60000}}"
exec java -cp build/classes topk.Main "$PORT" "$WINDOW_MS"
