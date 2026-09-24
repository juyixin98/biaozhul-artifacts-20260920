#!/usr/bin/env bash
# Create the PostgreSQL role and databases used by the service and tests.
# Idempotent: safe to run repeatedly. Expects a local PostgreSQL reachable
# via peer authentication as the postgres OS user (Debian/Ubuntu default).
set -euo pipefail

PGUSER_SUPER="${PGUSER_SUPER:-postgres}"
ROLE="${DB_ROLE:-deadlock}"
PASSWORD="${DB_PASSWORD:-deadlock_pw_068}"
DBS=("${DB_NAME:-deadlock_db}" "${DB_NAME_TEST:-deadlock_db_http}")

psql_super() { sudo -u "$PGUSER_SUPER" psql -v ON_ERROR_STOP=1 "$@"; }

psql_super <<SQL
DO \$do\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${ROLE}') THEN
    CREATE ROLE ${ROLE} LOGIN PASSWORD '${PASSWORD}';
  ELSE
    ALTER ROLE ${ROLE} PASSWORD '${PASSWORD}';
  END IF;
END \$do\$;
SQL

for db in "${DBS[@]}"; do
  if ! psql_super -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    sudo -u "$PGUSER_SUPER" createdb -O "$ROLE" "$db"
    echo "created database $db"
  else
    echo "database $db already exists"
  fi
done

echo "role '$ROLE' and databases ${DBS[*]} are ready"
