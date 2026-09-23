#!/usr/bin/env bash
# 运行一个或多个请求 JSON 文件：
#   scripts/run.sh examples/01_rank.json
# 不带参数时从标准输入读取：
#   cat examples/01_rank.json | scripts/run.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CLASSES_DIR="$ROOT_DIR/build/classes"
if [ ! -d "$CLASSES_DIR" ]; then
  bash "$ROOT_DIR/scripts/build.sh"
fi

java -cp "$CLASSES_DIR" windowengine.Main "$@"
