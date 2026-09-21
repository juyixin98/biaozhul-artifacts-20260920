#!/usr/bin/env bash
set -euo pipefail

echo ">> waiting for database at ${CAREFORCE_DATABASE_URL%%\?*} ..."
python - <<'PY'
import os, time
import psycopg2
from urllib.parse import urlparse

url = os.environ["CAREFORCE_DATABASE_URL"].replace("postgresql+psycopg2://", "postgresql://")
parsed = urlparse(url)
dsn = {
    "host": parsed.hostname,
    "port": parsed.port or 5432,
    "user": parsed.username,
    "password": parsed.password,
    "dbname": parsed.path.lstrip("/"),
}
deadline = time.time() + 60
while True:
    try:
        psycopg2.connect(**dsn).close()
        print(">> database is ready")
        break
    except Exception as exc:  # noqa: BLE001
        if time.time() > deadline:
            raise
        print(f"   db not ready yet: {exc}; retrying")
        time.sleep(1)
PY

echo ">> running migrations"
alembic upgrade head

echo ">> starting API server"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
