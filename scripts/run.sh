#!/usr/bin/env bash
# Start the HTTP service. Default port 8080; override with --port.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d build/classes ]; then
  scripts/build.sh
fi
exec java -cp build/classes qsummary.HttpServerMain "$@"
