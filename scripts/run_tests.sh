#!/usr/bin/env bash
# 编译并运行全部自动化测试；任一用例失败则以非零码退出。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

bash scripts/build.sh

echo
echo "[test] running AllTests..."
java -cp build/classes:build/test-classes com.example.phrasesearch.tests.AllTests
