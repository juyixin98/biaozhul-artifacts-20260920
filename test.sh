#!/usr/bin/env bash
# 编译（若需要）并运行全部自动化测试。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build ] || [ "$#" -gt 0 ]; then
  ./build.sh
fi

java -cp build sessions.testing.TestRunner \
  sessions.TimerServiceTest \
  sessions.SessionWindowOperatorTest \
  sessions.ReferenceEquivalenceTest \
  sessions.JsonTest \
  sessions.ServiceTest
