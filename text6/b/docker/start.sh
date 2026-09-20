#!/usr/bin/env bash
set -euo pipefail

# Wait for PostgreSQL to accept connections.
python - <<'PY'
import os, socket, time, sys

url = os.environ.get("DATABASE_URL", "")
host = os.environ.get("DB_HOST", "db")
port = int(os.environ.get("DB_PORT", "5432"))
for attempt in range(60):
    try:
        with socket.create_connection((host, port), timeout=2):
            break
    except OSError:
        print(f"waiting for database {host}:{port} ({attempt + 1}/60)")
        time.sleep(1)
else:
    print("database did not become reachable", file=sys.stderr)
    sys.exit(1)
PY

alembic upgrade head

if [ "${SEED_DEMO_DATA:-1}" = "1" ]; then
  python -m app.scripts.seed || true
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
