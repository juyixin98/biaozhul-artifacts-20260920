#!/usr/bin/env bash
# Wait for MySQL, migrate, seed simulated data, warm order books, start API.
set -euo pipefail

echo "[entrypoint] waiting for database ${MYSQL_HOST:-db}:${MYSQL_PORT:-3306}..."
for _ in $(seq 1 60); do
    if nc -z "${MYSQL_HOST:-db}" "${MYSQL_PORT:-3306}" 2>/dev/null; then
        echo "[entrypoint] database is accepting connections"
        break
    fi
    sleep 1
done

echo "[entrypoint] applying migrations..."
python manage.py migrate --noinput

echo "[entrypoint] seeding simulated assets / markets / demo users..."
python manage.py seed_simulated --demo-users "${SEED_DEMO_USERS:-3}"

echo "[entrypoint] recovering in-memory order books from persisted state..."
python manage.py warm_books

echo "[entrypoint] reconciliation sanity check..."
python manage.py reconcile --quiet || true

echo "[entrypoint] starting: $*"
exec "$@"
