#!/usr/bin/env bash
# 运行全部自动化测试，任一失败则以非零状态码退出。
set -euo pipefail
cd "$(dirname "$0")"

JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"
JAVA="${JAVA:-$JAVA_BIN}"

./build.sh >/dev/null
exec "$JAVA" -cp out/classes:out/test-classes com.example.tvl.tests.TestRunner
