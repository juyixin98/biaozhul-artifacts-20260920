#!/usr/bin/env bash
# 运行窗口引擎：bin/run.sh [请求文件.json [输出文件.json]]
# 不带参数时从标准输入读取请求。
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}"
JAVA="$JAVA_HOME/bin/java"
[ -x "$JAVA" ] || JAVA="java"

if [ ! -d out/classes ]; then
    ./build.sh
fi
exec "$JAVA" -cp out/classes engine.Main "$@"
