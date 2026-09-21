#!/usr/bin/env bash
set -euo pipefail

# Wait for PostgreSQL to accept connections.
until python -c "
import os, sys, psycopg2
url = os.environ['DATABASE_URL'].replace('+psycopg2', '')
try:
    psycopg2.connect(url, connect_timeout=2).close()
except Exception:
    sys.exit(1)
" 2>/dev/null; do
  echo "waiting for database..."
  sleep 1
done

echo "Running Alembic migrations..."
alembic upgrade head

if [ "${SEED_DEMO_DATA:-true}" = "true" ]; then
  echo "Seeding demo data (idempotent)..."
  python -m app.seed || true
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
