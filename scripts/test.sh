#!/usr/bin/env bash
# 运行全部自动化测试（零依赖迷你测试框架）。
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/build.sh --all
java -cp build/classes:build/test-classes dev.example.cp.tests.TestCase
