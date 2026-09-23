#!/usr/bin/env bash
# 便捷运行入口：
#   ./run.sh serve [--port 8080]
#   ./run.sh run  samples/09_batch.json
#   cat req.json | ./run.sh stdin
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d out ]; then
  ./build.sh >/dev/null
fi

java -cp out tvl.Main "$@"
