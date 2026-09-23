#!/usr/bin/env bash
# Idempotent local PostgreSQL bootstrap for the TWAP service.
#
# Creates role "twap" (password "twappw") and database "twap". Schema is
# applied automatically by the server on startup, so this script only handles
# the role/database.
#
# Override defaults with environment variables:
#   PG_SUPERUSER=postgres PG_SUPERUSE_SUDO=1 \
#   TWAP_DB_USER=twap TWAP_DB_PASSWORD=twappw TWAP_DB_NAME=twap
set -euo pipefail

PG_SUPERUSER="${PG_SUPERUSER:-postgres}"
# On Debian/Ubuntu the OS postgres user owns the cluster; sudo is the usual way in.
PG_SUPERUSE_SUDO="${PG_SUPERUSE_SUDO:-1}"
DB_USER="${TWAP_DB_USER:-twap}"
DB_PASS="${TWAP_DB_PASSWORD:-twappw}"
DB_NAME="${TWAP_DB_NAME:-twap}"

psql_super() {
  if [[ "${PG_SUPERUSE_SUDO}" == "1" ]]; then
    sudo -n -u "${PG_SUPERUSER}" psql -v ON_ERROR_STOP=1 "$@"
  else
    psql -v ON_ERROR_STOP=1 -U "${PG_SUPERUSER}" "$@"
  fi
}

echo ">> Ensuring role ${DB_USER}"
psql_super -d postgres -tAc \
  "DO \$\$ BEGIN
     IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${DB_USER}') THEN
       CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}' CREATEDB;
     ELSE
       ALTER ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}' CREATEDB;
     END IF;
   END \$\$;"

echo ">> Ensuring database ${DB_NAME}"
psql_super -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
  || psql_super -d postgres -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
psql_super -d postgres -c "ALTER DATABASE ${DB_NAME} OWNER TO ${DB_USER};" || true

echo ">> Verifying TCP login"
PGPASSWORD="${DB_PASS}" psql -h 127.0.0.1 -U "${DB_USER}" -d "${DB_NAME}" -c \
  "SELECT current_user, current_database();"

echo ">> OK. DSN: postgres://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}?sslmode=disable"
