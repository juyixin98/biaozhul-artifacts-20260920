#!/bin/bash
# docker compose 初始化：额外创建测试库（主库 promo_atomic 由 POSTGRES_DB 创建）
set -e
for db in promo_atomic_test promo_atomic_http; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -v db="$db" <<-EOSQL
    SELECT 'CREATE DATABASE ' || :'db' || ' OWNER $POSTGRES_USER'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db')\gexec
EOSQL
done
