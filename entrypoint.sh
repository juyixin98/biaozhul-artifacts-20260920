#!/usr/bin/env bash
# 容器入口：
#   web       -> 等待数据库、启动恢复、跑 gunicorn
#   scheduler -> 等待数据库、启动恢复、跑后台调度循环
#   migrate   -> 执行数据库迁移后退出（由专用 migrator 服务独占运行）
set -euo pipefail

cd /app

echo "[entrypoint] 等待 MySQL ${MYSQL_HOST:-db}:${MYSQL_PORT:-3306} ..."
python - <<'PYEOF'
import os, sys, time

import pymysql

host = os.environ.get("MYSQL_HOST", "db")
port = int(os.environ.get("MYSQL_PORT", "3306"))
user = os.environ.get("MYSQL_USER", "scheduler")
password = os.environ.get("MYSQL_PASSWORD", "scheduler_pw")
database = os.environ.get("MYSQL_DATABASE", "mlops_scheduler")

for attempt in range(60):
    try:
        conn = pymysql.connect(host=host, port=port, user=user,
                               password=password, database=database)
        conn.close()
        print(f"[entrypoint] MySQL 已就绪 ({host}:{port})")
        sys.exit(0)
    except Exception as exc:
        print(f"[entrypoint] 等待数据库... ({attempt + 1}/60) {exc}")
        time.sleep(2)
print("[entrypoint] 数据库不可用，退出")
sys.exit(1)
PYEOF

ROLE="${1:-web}"

if [ "$ROLE" = "migrate" ]; then
  echo "[entrypoint] 执行数据库迁移（独占）"
  python manage.py migrate --noinput
  echo "[entrypoint] 迁移完成，退出"
  exit 0
fi

# web / scheduler 不自行迁移（schema 由 migrator 统一完成），仅做启动恢复。
echo "[entrypoint] 启动恢复：从数据库重建分配视图"
python manage.py recover_state

case "$ROLE" in
  web)
    echo "[entrypoint] 启动 API (gunicorn :8000)"
    exec gunicorn config.wsgi:application \
      --bind 0.0.0.0:8000 --workers "${GUNICORN_WORKERS:-3}" --timeout 60
    ;;
  scheduler)
    echo "[entrypoint] 启动后台调度循环"
    exec python manage.py run_scheduler --interval "${SCHED_INTERVAL:-3}"
    ;;
  *)
    exec "$@"
    ;;
esac
