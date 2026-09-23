#!/usr/bin/env bash
# 启动 HTTP 服务。参数原样透传给 cep.Main，例如：
#   ./run.sh --port=9090 --data-dir=/tmp/cep-data
set -euo pipefail
cd "$(dirname "$0")"

if [[ -n "${JAVA_HOME:-}" ]]; then
  JAVA="$JAVA_HOME/bin/java"
else
  JAVA="$(command -v java)"
fi
if [[ -z "${JAVA:-}" ]]; then
  echo "错误: 未找到 java。请安装 JDK 17+，或设置 JAVA_HOME。" >&2
  exit 1
fi

if [[ ! -d build/classes ]]; then
  echo "build/classes 不存在，先执行 ./build.sh" >&2
  exit 1
fi

exec "$JAVA" -cp build/classes cep.Main "$@"
