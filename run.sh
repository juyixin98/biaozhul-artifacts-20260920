#!/usr/bin/env bash
# Run one JSON request:
#   ./run.sh examples/inner-small.json [out/response.json]
#   ./run.sh plan examples/spill-left.json     # plan only, no execution
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d build/classes ]; then
  ./build.sh
fi

exec java -cp build/classes partjoin.Main "$@"
