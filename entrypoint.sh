#!/bin/sh
set -e

case "$1" in
  web)
    echo "Waiting for database..."
    python - <<'PY'
import os, time, psycopg2

url = os.environ["CLOUDGATE_DATABASE_URL"]
# convert sqlalchemy driver for raw psycopg2
raw = url.replace("postgresql+psycopg2://", "postgresql://")
deadline = time.time() + 60
while True:
    try:
        psycopg2.connect(raw).close()
        break
    except Exception as exc:
        if time.time() > deadline:
            raise
        print(f"db not ready: {exc}; retrying...")
        time.sleep(1)
PY
    echo "Running migrations..."
    alembic upgrade head
    echo "Starting API..."
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  *)
    exec "$@"
    ;;
esac
