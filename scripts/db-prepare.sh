#!/usr/bin/env bash
# 准备本机 PostgreSQL 角色与数据库（apt 安装的 PostgreSQL，本地 peer 认证）。
# 若已设置 DATABASE_URL 则跳过（例如使用 docker compose 时）。
set -euo pipefail

if [[ -n "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is set, skipping local role/db creation"
  exit 0
fi

ROLE=${PGUSER:-promo}
PWD_=${PGPASSWORD:-promo_dev_pwd}
DBS=(promo_atomic promo_atomic_test promo_atomic_http)

if ! command -v psql >/dev/null; then
  echo "psql not found; use docker compose up -d and set DATABASE_URL" >&2
  exit 1
fi

sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${ROLE}') THEN
    CREATE ROLE ${ROLE} LOGIN PASSWORD '${PWD_}';
  END IF;
END \$\$;
SQL

for db in "${DBS[@]}"; do
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
    sudo -u postgres createdb -O "$ROLE" "$db"
  fi
  echo "database $db ready"
done
