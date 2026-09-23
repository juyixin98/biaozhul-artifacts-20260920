#!/usr/bin/env bash
# 运行全部自动化测试。先编译，再执行 TestRunner。
set -euo pipefail
cd "$(dirname "$0")"

./build.sh
JAVA_BIN="${JAVA_HOME:+$JAVA_HOME/bin/}java"
exec $JAVA_BIN -cp build/classes:build/test com.example.intervalindex.TestRunner
