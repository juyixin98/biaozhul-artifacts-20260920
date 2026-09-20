#!/usr/bin/env bash
set -euo pipefail

# Wait for PostgreSQL to accept connections.
python - <<'PY'
import os, time
import psycopg2

url = os.environ["WF_DATABASE_URL"]
# strip sqlalchemy driver for the raw driver
raw = url.replace("postgresql+psycopg2://", "postgresql://")
for attempt in range(60):
    try:
        psycopg2.connect(raw, connect_timeout=2).close()
        break
    except Exception:
        time.sleep(1)
else:
    raise SystemExit("database not reachable")
PY

echo "Applying database migrations..."
alembic upgrade head

if [ "${WF_SEED_DEMO:-false}" = "true" ]; then
  echo "Seeding demo template..."
  python -c "from app.db import SessionLocal; from app.seed import seed_demo; db=SessionLocal(); seed_demo(db); db.close()"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
