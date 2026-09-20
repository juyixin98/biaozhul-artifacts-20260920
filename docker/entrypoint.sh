#!/bin/sh
set -e

cd /var/www

echo "[entrypoint] waiting for database ${DB_HOST:-db}:${DB_PORT:-3306} ..."
php bin/wait_for_db.php

echo "[entrypoint] running migrations ..."
php bin/migrate.php

if [ "${SEED_ON_START:-1}" = "1" ]; then
    echo "[entrypoint] seeding sample data ..."
    php bin/seed.php || echo "[entrypoint] seed skipped/failed (data may already exist)"
fi

case "${1:-serve}" in
    serve)
        echo "[entrypoint] starting PHP server on :8080"
        exec php -S 0.0.0.0:8080 -t public public/index.php
        ;;
    *)
        exec "$@"
        ;;
esac
