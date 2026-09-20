#!/usr/bin/env bash
set -euo pipefail

# Wait for PostgreSQL, run migrations, optionally seed demo data, then exec CMD.
echo "Waiting for database..."
python - <<'PY'
import os, time, psycopg

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
deadline = time.time() + 60
while True:
    try:
        with psycopg.connect(url, connect_timeout=2) as conn:
            conn.execute("select 1")
        break
    except Exception as exc:
        if time.time() > deadline:
            raise
        print(f"  db not ready ({exc}); retrying...")
        time.sleep(1)
print("database is ready")
PY

echo "Running alembic migrations..."
alembic upgrade head

if [ "${SEED_DEMO:-0}" = "1" ]; then
    echo "Seeding demo data..."
    python seed_demo.py || true
fi

exec "$@"
