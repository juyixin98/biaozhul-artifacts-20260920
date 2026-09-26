#!/usr/bin/env bash
# 编译（跳过测试）并运行一个 JSON 请求样例：
#   ./run.sh samples/02-monthly-31st-leap.json
# 无参数或参数为 - 时从标准输入读取 JSON。
set -euo pipefail
cd "$(dirname "$0")"

mvn -o -q dependency:build-classpath -Dmdep.outputFile=target/cp.txt >/dev/null 2>&1 || \
  mvn -q dependency:build-classpath -Dmdep.outputFile=target/cp.txt >/dev/null
mvn -o -q package -DskipTests >/dev/null 2>&1 || mvn -q package -DskipTests >/dev/null

java -cp "target/recurrence-backend-1.0.0.jar:$(cat target/cp.txt)" com.example.recur.Main "${1:-}"
