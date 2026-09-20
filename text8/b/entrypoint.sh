#!/bin/sh
set -e

if [ "$1" = "serve" ]; then
    echo "Waiting for PostgreSQL..."
    python - <<'PY'
import os, time, psycopg2

url = os.environ["DATABASE_URL"]
# convert sqlalchemy url to libpq kwargs
rest = url.split("://", 1)[1]
creds, hostpart = rest.split("@", 1)
user, password = creds.split(":", 1)
hostport, dbname = hostpart.split("/", 1)
if ":" in hostport:
    host, port = hostport.split(":", 1)
else:
    host, port = hostport, "5432"

for i in range(60):
    try:
        psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname).close()
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("PostgreSQL not reachable")
PY

    echo "Running migrations..."
    alembic upgrade head

    echo "Starting API server..."
    exec uvicorn workflow_engine.main:app --host 0.0.0.0 --port 8000
fi

exec "$@"
