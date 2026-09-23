#!/usr/bin/env bash
# 初始化本地 PostgreSQL：创建 rollout 角色与数据库，应用迁移。
# 需要本机 sudo 权限访问 postgres 系统账号。
set -euo pipefail

cd "$(dirname "$0")/.."

sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='rollout') THEN
    CREATE ROLE rollout LOGIN PASSWORD 'rollout';
  END IF;
END $$;
SQL

for db in rollout rollout_test; do
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
    sudo -u postgres createdb -O rollout "$db"
    echo "created database $db"
  else
    echo "database $db already exists"
  fi
  # 既有对象（可能由 postgres 创建）统一移交 rollout，保证迁移幂等
  sudo -u postgres psql -d "$db" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO rollout', r.tablename);
  END LOOP;
  FOR r IN SELECT sequencename FROM pg_sequences WHERE schemaname='public' LOOP
    EXECUTE format('ALTER SEQUENCE public.%I OWNER TO rollout', r.sequencename);
  END LOOP;
END $$;
SQL
  # 以 rollout 身份应用迁移（对象属主正确，API 进程可重复执行）
  sudo -u postgres psql -d "$db" -v ON_ERROR_STOP=1 \
    -c "SET ROLE rollout;" -f migrations/0001_init.sql
  echo "migrations applied to $db"
done
