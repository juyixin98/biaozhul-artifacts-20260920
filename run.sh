#!/usr/bin/env bash
# Local service entrypoint. Fixtures are generated on first run if missing.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f "fixtures/tiny-matmul-demo.weights.npz" ]; then
  python3 scripts/generate_fixtures.py
fi

exec python3 -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
