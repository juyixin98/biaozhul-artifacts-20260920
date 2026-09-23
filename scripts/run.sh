#!/usr/bin/env bash
# 启动 HTTP 服务。
# 用法: scripts/run.sh [port] [windowMs]
# 例:   scripts/run.sh 8080 10000
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
  JAVA="$JAVA_HOME/bin/java"
elif command -v java >/dev/null 2>&1; then
  JAVA="java"
elif [ -x "$HOME/jdk21/usr/lib/jvm/java-21-openjdk-amd64/bin/java" ]; then
  JAVA="$HOME/jdk21/usr/lib/jvm/java-21-openjdk-amd64/bin/java"
else
  echo "错误: 找不到 java。请安装 JDK 11+ 或设置 JAVA_HOME。" >&2
  exit 1
fi

[ -d build/classes ] || scripts/build.sh

exec "$JAVA" -cp build/classes topk.TopKHttpServer "${1:-${TOPK_PORT:-8080}}" "${2:-${TOPK_WINDOW_MS:-10000}}"
