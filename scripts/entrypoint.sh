#!/usr/bin/env bash
# Wait for the database, apply migrations, optionally seed, then serve.
set -euo pipefail

python - <<'PY'
import time
import psycopg2
from app.config import get_settings

url = get_settings().database_url.replace("postgresql+psycopg2://", "postgresql://")
for attempt in range(60):
    try:
        psycopg2.connect(url).close()
        break
    except Exception:
        print("waiting for database...")
        time.sleep(1)
else:
    raise SystemExit("database not reachable")
PY

alembic upgrade head

if [[ "${1:-}" == "--seed" ]]; then
  python -m app.seed || true
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
