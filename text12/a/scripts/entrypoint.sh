#!/bin/sh
# 启动入口：等待数据库就绪 -> 执行数据库迁移 -> 启动控制平面 API
set -e

echo "[cloudgate] 等待数据库 ${CLOUDGATE_DATABASE_URL%%\?*} ..."
python - <<'PY'
import os, time
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

url = os.environ["CLOUDGATE_DATABASE_URL"]
engine = create_engine(url, pool_pre_ping=True)
for i in range(60):
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("[cloudgate] 数据库已就绪")
        break
    except OperationalError:
        time.sleep(1)
else:
    raise SystemExit("[cloudgate] 数据库 60 秒内不可用，退出")
PY

echo "[cloudgate] 执行数据库迁移 ..."
alembic upgrade head

echo "[cloudgate] 启动控制平面 ..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${UVICORN_PORT:-8000}"
