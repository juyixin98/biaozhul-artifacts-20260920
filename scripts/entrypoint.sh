#!/usr/bin/env bash
# Container entrypoint: wait for Postgres, ensure demo data exists, then exec.
set -euo pipefail

# Generate demo datasets into the whitelisted directory if absent.
if [ ! -f "${DATA_WHITELIST:-/data}/iris_demo.csv" ]; then
    echo "[entrypoint] generating demo data in ${DATA_WHITELIST:-/data}"
    python scripts/generate_demo_data.py || true
fi

# Wait for PostgreSQL to accept connections (compose healthcheck usually
# covers this, but this makes standalone ``docker run`` robust too).
python - <<'PY'
import os, sys, time
import psycopg2
url = os.environ.get("DATABASE_URL")
if url:
    # SQLAlchemy URL -> psycopg2 dsn
    dsn = url.replace("postgresql+psycopg2://", "postgresql://")
else:
    dsn = (f"postgresql://{os.environ.get('POSTGRES_USER','nn')}:"
           f"{os.environ.get('POSTGRES_PASSWORD','nn')}@"
           f"{os.environ.get('POSTGRES_HOST','db')}:"
           f"{os.environ.get('POSTGRES_PORT','5432')}/"
           f"{os.environ.get('POSTGRES_DB','nn_training')}")
for attempt in range(60):
    try:
        psycopg2.connect(dsn).close()
        print("[entrypoint] database is ready")
        break
    except Exception as exc:  # noqa: BLE001
        print(f"[entrypoint] waiting for database... ({attempt + 1}/60) {exc}")
        time.sleep(1)
else:
    print("[entrypoint] database never became ready", file=sys.stderr)
    sys.exit(1)
PY

exec "$@"
