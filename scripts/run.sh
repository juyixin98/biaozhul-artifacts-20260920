#!/usr/bin/env bash
# Start the server. Env overrides: SEQCEP_PORT (default 8080), SEQCEP_DATA_DIR (default ./data).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d build/classes ]; then
  ./scripts/build.sh
fi

exec java -cp build/classes seqcep.Main
