#!/usr/bin/env bash
# 启动服务。环境变量：
#   TXFLOW_HOST   监听地址，默认 127.0.0.1（纯本地服务，不建议暴露到非可信网络）
#   TXFLOW_PORT   端口，默认 8080
#   TXFLOW_DATA   数据目录，默认 ./txflow-data
set -euo pipefail
cd "$(dirname "$0")/.."

JAVA_HOME="${JAVA_HOME:-$HOME/jdk17/usr/lib/jvm/java-17-openjdk-amd64}"
JAVA="$JAVA_HOME/bin/java"
if [ ! -x "$JAVA" ]; then
  JAVA="$(command -v java)"
fi

"$JAVA" -cp out com.example.txflow.HttpApiServer
