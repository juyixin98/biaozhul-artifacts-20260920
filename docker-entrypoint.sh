#!/usr/bin/env bash
set -euo pipefail

# Wait for PostgreSQL to accept connections.
python - <<'PY'
import os
import time

import psycopg

url = os.environ["CIVICLEDGER_DATABASE_URL"].replace("+psycopg", "")
deadline = time.time() + 60
while True:
    try:
        with psycopg.connect(url, connect_timeout=3):
            break
    except Exception as exc:  # noqa: BLE001
        if time.time() > deadline:
            raise
        print(f"Waiting for database... ({exc})")
        time.sleep(1)
print("Database is ready.")
PY

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  alembic upgrade head
  if [ "${RUN_SEED:-0}" = "1" ]; then
    python -m app.seed
  fi
fi

exec "$@"
