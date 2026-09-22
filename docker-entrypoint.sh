#!/bin/sh
set -e

echo "Waiting for MySQL at ${DB_HOST:-db}:${DB_PORT:-3306} ..."
until node -e "
const mysql = require('mysql2/promise');
mysql.createConnection({
  host: process.env.DB_HOST || 'db',
  port: Number(process.env.DB_PORT || 3306),
  user: process.env.DB_USER || 'root',
  password: process.env.DB_PASSWORD || 'careops',
}).then(c => c.end()).then(() => process.exit(0)).catch(() => process.exit(1));
" 2>/dev/null; do
  sleep 2
done
echo "MySQL is up."

echo "Running migrations ..."
node src/db/migrate.js

if [ "${ENABLE_DEMO_SEED:-false}" = "true" ]; then
  echo "Seeding demo data ..."
  node src/seeders/demo-data.js || true
fi

exec "$@"
