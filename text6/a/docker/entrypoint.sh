#!/usr/bin/env bash
set -euo pipefail

# Wait for PostgreSQL, apply migrations, then run the container command.
echo "[entrypoint] waiting for database..."
until pg_isready -h "${POSTGRES_HOST:-db}" -p "${POSTGRES_PORT:-5432}" -q; do
    sleep 1
done
echo "[entrypoint] database is ready"

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    echo "[entrypoint] applying migrations..."
    alembic upgrade head
fi

exec "$@"
