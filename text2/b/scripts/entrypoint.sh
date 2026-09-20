#!/usr/bin/env bash
# Apply database migrations, optionally load sample data, then exec the API.
set -euo pipefail

echo "Running Alembic migrations..."
alembic upgrade head

if [[ "${RUN_SEED:-false}" == "true" ]]; then
  echo "Loading sample data (idempotent)..."
  python -m scripts.seed || echo "seed skipped (already present)"
fi

exec "$@"
