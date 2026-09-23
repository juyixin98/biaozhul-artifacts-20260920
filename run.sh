#!/usr/bin/env bash
# 用法: ./run.sh examples/request_filter.json
# 执行一次查询并打印 JSON 结果（也可: cat req.json | ./run.sh -）。
set -euo pipefail
cd "$(dirname "$0")"
[ $# -ge 1 ] || { echo "用法: $0 <request.json | ->"; exit 2; }
java -cp target/classes vecq.Main run "$@"
