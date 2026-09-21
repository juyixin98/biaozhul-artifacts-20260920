#!/usr/bin/env bash
set -euo pipefail

# Wait for PostgreSQL to accept connections.
until python -c "
import os, psycopg
url = os.environ['SENTINEL_DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://')
psycopg.connect(url, connect_timeout=2).close()
" 2>/dev/null; do
  echo "waiting for database..."
  sleep 1
done

echo "running migrations"
alembic upgrade head

if [ "${SENTINEL_SEED_DEMO:-0}" = "1" ]; then
  echo "seeding demo data"
  python -m scripts.seed_demo || echo "seed skipped (already present?)"
fi

exec "$@"
