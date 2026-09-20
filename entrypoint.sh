#!/usr/bin/env bash
# Apply database migrations, optionally seed demo data, then exec the given command.
set -euo pipefail

echo "[entrypoint] waiting for PostgreSQL..."
python - <<'PY'
import os, time, psycopg2

url = os.environ["CV_DATABASE_URL"].replace("postgresql+psycopg2://", "postgresql://")
deadline = time.time() + 60
while True:
    try:
        psycopg2.connect(url).close()
        break
    except Exception as exc:  # noqa: BLE001
        if time.time() > deadline:
            raise
        print("[entrypoint]   db not ready, retrying...", exc.__class__.__name__)
        time.sleep(1)
print("[entrypoint] PostgreSQL is ready.")
PY

echo "[entrypoint] running migrations..."
alembic upgrade head

if [ "${CV_SEED_DEMO:-true}" = "true" ]; then
  echo "[entrypoint] seeding demo organizations, keys and scenario..."
  python -m app.bootstrap
fi

echo "[entrypoint] starting: $*"
exec "$@"
