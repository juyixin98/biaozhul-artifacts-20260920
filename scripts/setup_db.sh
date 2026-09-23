#!/usr/bin/env bash
# 初始化本地 PostgreSQL:创建角色、业务库、测试库并应用 schema。
# 默认通过 sudo 以 postgres 超级用户执行;可用 PGADMIN 覆盖,例如:
#   PGADMIN="psql postgres://postgres:secret@127.0.0.1:5432/postgres" ./scripts/setup_db.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PGADMIN=${PGADMIN:-"sudo -n -u postgres psql -v ON_ERROR_STOP=1"}
APP_DB=${APP_DB:-mqtt_telemetry}
TEST_DB=${TEST_DB:-mqtt_telemetry_test}

$PGADMIN <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mqtt') THEN
    CREATE ROLE mqtt LOGIN PASSWORD 'mqtt_secret';
  END IF;
END
$$;
SQL

for db in "$APP_DB" "$TEST_DB"; do
  if ! $PGADMIN -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1; then
    $PGADMIN -c "CREATE DATABASE $db OWNER mqtt"
    echo "已创建数据库 $db"
  fi
done

psql "postgres://mqtt:mqtt_secret@127.0.0.1:5432/$APP_DB" -v ON_ERROR_STOP=1 -q -f db/schema.sql
psql "postgres://mqtt:mqtt_secret@127.0.0.1:5432/$TEST_DB" -v ON_ERROR_STOP=1 -q -f db/schema.sql
echo "数据库就绪: $APP_DB / $TEST_DB"
