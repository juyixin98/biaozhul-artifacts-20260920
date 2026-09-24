#!/usr/bin/env bash
# Create the application role and databases used by the registry and its
# integration tests. Idempotent. Run as a Postgres superuser (e.g. via sudo).
#
#   sudo -u postgres bash scripts/setup-db.sh
#
# Override defaults with ROLE / PASSWORD / APP_DB / TEST_DB env vars.
set -euo pipefail
ROLE="${ROLE:-registry}"
PASSWORD="${PASSWORD:-registry_pw}"
APP_DB="${APP_DB:-registry}"
TEST_DB="${TEST_DB:-registry_test}"

psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${ROLE}') THEN
    CREATE ROLE ${ROLE} LOGIN PASSWORD '${PASSWORD}';
  ELSE
    ALTER ROLE ${ROLE} LOGIN PASSWORD '${PASSWORD}';
  END IF;
END
\$\$;
SQL

for db in "$APP_DB" "$TEST_DB"; do
  if ! psql -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
    psql -c "CREATE DATABASE $db OWNER $ROLE;"
  else
    echo "database $db already exists"
  fi
done

echo "done. connect with:"
echo "  postgres://${ROLE}:***@127.0.0.1:5432/${APP_DB}?sslmode=disable"
