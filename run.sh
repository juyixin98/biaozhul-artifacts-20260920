#!/usr/bin/env bash
# 运行服务。参数透传给 drvb.Main，例如：
#   ./run.sh --port=8080 --clock=manual --allowed-lateness=5000 --retention-horizon=60000
set -euo pipefail
cd "$(dirname "$0")"
exec java -cp build/classes drvb.Main "$@"
