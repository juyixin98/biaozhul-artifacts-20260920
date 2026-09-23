#!/usr/bin/env bash
# 运行全部自动化测试。
set -euo pipefail
cd "$(dirname "$0")"

JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-amd64}"
JAVA="$JAVA_HOME/bin/java"
[ -x "$JAVA" ] || JAVA="java"

./build.sh
echo ">> 运行测试..."
"$JAVA" -cp out/classes:out/test-classes test.RunAllTests
