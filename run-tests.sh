#!/usr/bin/env bash
# 编译（若需要）并运行全部自动化测试，测试失败时以非 0 退出。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MAIN_OUT=build/classes
TEST_OUT=build/test-classes

if [ ! -d "$MAIN_OUT" ] || [ ! -d "$TEST_OUT" ]; then
    bash build.sh
fi

java -cp "$MAIN_OUT:$TEST_OUT" tvl.TestRunner
