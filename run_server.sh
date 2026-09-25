#!/usr/bin/env bash
# Start the TaintLang JSON/HTTP service (default 127.0.0.1:8080).
set -euo pipefail
cd "$(dirname "$0")"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
exec python3 -m taintlang.cli serve --host "$HOST" --port "$PORT"
