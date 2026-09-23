#!/usr/bin/env bash
# 运行一次连接查询：./scripts/run.sh examples/inner-spill.json
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f out/classes/phj/Main.class ] || ./scripts/build.sh
exec java -cp out/classes phj.Main "$@"
