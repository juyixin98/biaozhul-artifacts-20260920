#!/usr/bin/env bash
# Creates the local bt065b role and btree065b database used by the service and the
# tests. Idempotent. Requires peer-access as the postgres OS user (sudo).
set -euo pipefail

ROLE=bt065b
PASSWORD=bt065b
DB=btree065b

sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${ROLE}') THEN
    CREATE ROLE ${ROLE} LOGIN PASSWORD '${PASSWORD}';
  ELSE
    ALTER ROLE ${ROLE} WITH PASSWORD '${PASSWORD}';
  END IF;
END \$\$;
SQL

if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB}'" | grep -q 1; then
  sudo -u postgres createdb -O "${ROLE}" "${DB}"
  echo "created database ${DB}"
else
  echo "database ${DB} already exists"
fi

echo "connection: postgres://${ROLE}:${PASSWORD}@localhost:5432/${DB}?sslmode=disable"
