#!/usr/bin/env bash
set -euo pipefail

# 1. Apply database migrations (retries until PostgreSQL is reachable)
echo "[entrypoint] running migrations..."
python - <<'PY'
import time
import sys
from alembic.config import Config
from alembic import command

cfg = Config("alembic.ini")
for attempt in range(30):
    try:
        command.upgrade(cfg, "head")
        break
    except Exception as exc:  # noqa: BLE001
        print(f"[entrypoint] database not ready ({attempt + 1}/30): {exc}")
        time.sleep(2)
else:
    print("[entrypoint] could not reach the database", file=sys.stderr)
    sys.exit(1)
PY

# 2. Start the API (the timeout-escalation worker runs in the same process)
echo "[entrypoint] starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
