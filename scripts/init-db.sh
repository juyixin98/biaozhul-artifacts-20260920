#!/usr/bin/env bash
# Create the application role and database locally (idempotent).
set -euo pipefail

ROLE="${SYN_ROLE:-synaptic}"
PASS="${SYN_PASS:-synaptic}"
DB="${SYN_DB:-synapticgo}"

SUDO=""
if [ "$(id -u)" != "0" ]; then
  SUDO="sudo -u postgres"
fi

$SUDO psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${ROLE}') THEN
    CREATE ROLE ${ROLE} LOGIN PASSWORD '${PASS}' CREATEDB;
  ELSE
    ALTER ROLE ${ROLE} LOGIN PASSWORD '${PASS}' CREATEDB;
  END IF;
END \$\$;
SQL

# Create the database if it does not exist yet.
if ! psql "postgres://${ROLE}:${PASS}@localhost:5432/postgres?sslmode=disable" \
      -tAc "SELECT 1 FROM pg_database WHERE datname='${DB}'" | grep -q 1; then
  $SUDO psql -c "CREATE DATABASE ${DB} OWNER ${ROLE}"
  echo "created database ${DB}"
else
  echo "database ${DB} already exists"
fi
