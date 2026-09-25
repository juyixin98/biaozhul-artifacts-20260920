#!/usr/bin/env bash
# 启动 JSON HTTP 服务。可选参数透传给 Main，例如：./run.sh --port 9090
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build/classes ]; then
    ./build.sh
fi

exec java -Dfile.encoding=UTF-8 -cp build/classes booleansearch.Main "$@"
