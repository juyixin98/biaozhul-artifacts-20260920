#!/usr/bin/env bash
# End-to-end smoke demo on the CLI:
#   fresh train with 2 processes -> validate/reshard to 3 -> restore+1 step parity
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-}:${ROOT}/src"

python3 -m adam_shard --config config/default.json init-data
COMMIT="$(python3 -m adam_shard --config config/default.json train --world-size 2 --steps 6 \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["commit_id"])')"
echo "Trained commit: ${COMMIT}"
python3 -m adam_shard --config config/default.json validate "${COMMIT}" --target-world-size 3
python3 -m adam_shard --config config/default.json verify-parity "${COMMIT}" --target-world-size 3
