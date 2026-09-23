#!/usr/bin/env bash
# 运行引擎：./scripts/run.sh samples/skew3.json [--export-dir build/out]
# 也可用 - 从标准输入读取请求。
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -lt 1 ]; then
  echo "用法: $0 <request.json|-> [--export-dir DIR]" >&2
  exit 2
fi
if [ ! -d build/classes ]; then
  ./scripts/build.sh >/dev/null
fi
java -cp build/classes joinopt.Main "$@"
