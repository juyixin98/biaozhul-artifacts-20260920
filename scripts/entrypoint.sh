#!/usr/bin/env bash
# Wait for PostgreSQL, apply migrations, then exec the container command.
set -euo pipefail

# The app reads VAULT_DATABASE_URL; tolerate DATABASE_URL too for plain run.
if [ -z "${VAULT_DATABASE_URL:-}" ] && [ -n "${DATABASE_URL:-}" ]; then
  export VAULT_DATABASE_URL="$DATABASE_URL"
fi
: "${VAULT_DATABASE_URL:?VAULT_DATABASE_URL is required}"

python - <<'PY'
import os, sys, time
from sqlalchemy import create_engine, text

url = os.environ["VAULT_DATABASE_URL"]
engine = create_engine(url, pool_pre_ping=True)
deadline = time.time() + 60
last_err = None
while time.time() < deadline:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("database is ready")
        break
    except Exception as exc:  # noqa: BLE001
        last_err = exc
        print(f"waiting for database... ({exc.__class__.__name__})")
        time.sleep(1)
else:
    print(f"database not reachable: {last_err}", file=sys.stderr)
    sys.exit(1)
PY

echo "applying migrations"
alembic upgrade head

exec "$@"
