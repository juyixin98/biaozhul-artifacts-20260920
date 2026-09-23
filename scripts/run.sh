#!/usr/bin/env bash
# 运行一个 JSON 查询请求：
#   scripts/run.sh samples/01_leftjoin_right_filter.json
#   scripts/run.sh samples/02_right_null_filter.json --export out/export.json
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d out ] || [ -z "$(find out -name '*.class' -print -quit)" ]; then
  scripts/build.sh
fi
java -cp out ppd.Main "$@"
