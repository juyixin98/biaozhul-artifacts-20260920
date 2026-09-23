#!/usr/bin/env bash
# 启动 HTTP JSON 服务：./scripts/serve.sh [port]  （默认 8080）
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f out/classes/phj/Main.class ] || ./scripts/build.sh
exec java -cp out/classes phj.server.HttpServerMain "$@"
