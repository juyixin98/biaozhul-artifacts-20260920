#!/usr/bin/env bash
set -euo pipefail

# 解析 DATABASE_URL 中的主机/端口，等待 PostgreSQL 接受连接
python - <<'PY'
import os
import time

import psycopg2

url = os.environ["DATABASE_URL"]
deadline = time.time() + 60
last = None
while time.time() < deadline:
    try:
        raw_url = os.environ["DATABASE_URL"].replace("+psycopg2", "")
        psycopg2.connect(raw_url, connect_timeout=2).close()
        print("database is ready")
        break
    except Exception as exc:  # noqa: BLE001
        last = exc
        time.sleep(1)
else:
    raise SystemExit(f"database not ready: {last}")
PY

echo "running migrations..."
alembic upgrade head

if "${SEED_DEMO:-false}" = "true"; then
  echo "seeding demo template..."
  python -m scripts.seed_demo --publish || echo "demo template already exists, skipped"
fi

exec "$@"
