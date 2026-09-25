#!/usr/bin/env bash
# 运行全部自动化测试
set -euo pipefail
cd "$(dirname "$0")"
java -cp out/main:out/test boolsearch.TestRunner
