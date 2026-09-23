#!/usr/bin/env bash
# 运行命令行验收演示（五个场景，无需启动服务）。
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
java -cp "$ROOT_DIR/build/classes" com.tjoin.demo.Demo
