#!/usr/bin/env bash
# 容器入口：等待 MySQL -> 迁移 -> 初始化模拟数据 -> 启动服务
set -euo pipefail

python - <<'PY'
import os, time, sys
import pymysql

host = os.environ.get("DB_HOST", "db")
port = int(os.environ.get("DB_PORT", "3306"))
user = os.environ.get("DB_USER", "cryptolaunch")
password = os.environ.get("DB_PASSWORD", "cryptolaunch")
name = os.environ.get("DB_NAME", "cryptolaunch")

for i in range(60):
    try:
        conn = pymysql.connect(host=host, port=port, user=user, password=password)
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{name}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.close()
        print("database is ready")
        break
    except Exception as exc:  # noqa: BLE001
        print(f"waiting for database ({i + 1}/60): {exc}", flush=True)
        time.sleep(1)
else:
    print("database not reachable", file=sys.stderr)
    sys.exit(1)
PY

python manage.py migrate --noinput
python manage.py init_sim_data

# wsgi 模块加载时会从数据库重建内存订单簿
exec gunicorn cryptolaunch.wsgi:application \
    --bind 0.0.0.0:8000 \
    --worker-class gthread \
    --workers 1 \
    --threads 8 \
    --access-logfile - \
    --error-logfile -
