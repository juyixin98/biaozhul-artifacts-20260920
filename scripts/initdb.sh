#!/usr/bin/env bash
# Provision the compgw role and databases in a local PostgreSQL cluster.
# Uses peer auth via the postgres OS user. Idempotent.
set -euo pipefail

DBUSER="${COMPGW_PG_USER:-compgw}"
DBPASS="${COMPGW_PG_PASSWORD:-compgw}"
DBS="${COMPGW_PG_DATABASES:-compgw compgw_test}"

psql_super() {
  if [ "$(id -un)" = "postgres" ]; then
    psql -v ON_ERROR_STOP=1 "$@"
  else
    sudo -u postgres psql -v ON_ERROR_STOP=1 "$@"
  fi
}

psql_super <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${DBUSER}') THEN
    CREATE ROLE ${DBUSER} LOGIN PASSWORD '${DBPASS}';
  END IF;
END \$\$;
SQL

for db in $DBS; do
  if ! psql_super -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    if [ "$(id -un)" = "postgres" ]; then
      createdb -O "$DBUSER" "$db"
    else
      sudo -u postgres createdb -O "$DBUSER" "$db"
    fi
    echo "created database $db"
  else
    echo "database $db already exists"
  fi
done
echo "postgres provisioning complete"
