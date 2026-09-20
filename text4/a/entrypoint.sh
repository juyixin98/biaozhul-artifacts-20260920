#!/usr/bin/env bash
set -euo pipefail

# Wait for PostgreSQL without pulling in a third-party container image.
until pg_isready -h "${POSTGRES_HOST:-db}" -p "${POSTGRES_PORT:-5432}" -U "${POSTGRES_USER:-skillpulse}" >/dev/null 2>&1; do
  echo "waiting for database..."
  sleep 1
done

alembic upgrade head

if [ "${SEED_DEMO:-0}" = "1" ]; then
  python -m app.seed || true
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
