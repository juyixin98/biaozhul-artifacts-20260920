#!/usr/bin/env bash
# Wait for MySQL, apply migrations, then run the container's main command.
set -e

: "${DB_HOST:=db}"
: "${DB_PORT:=3306}"

echo "[entrypoint] waiting for ${DB_HOST}:${DB_PORT} ..."
for _ in $(seq 1 60); do
    if nc -z "${DB_HOST}" "${DB_PORT}" 2>/dev/null; then
        echo "[entrypoint] database is reachable"
        break
    fi
    sleep 2
done

python manage.py migrate --noinput

exec "$@"
