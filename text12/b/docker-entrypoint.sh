#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   docker-entrypoint.sh web           -> migrate + seed + uvicorn
#   docker-entrypoint.sh tests         -> wait for db, migrate a fresh test DB, run pytest
#   docker-entrypoint.sh demo          -> wait for app, run the end-to-end demo script
#   docker-entrypoint.sh <other cmd>   -> exec the command

ROLE="${1:-web}"

case "$ROLE" in
  web)
    echo "[entrypoint] applying migrations"
    alembic upgrade head
    echo "[entrypoint] starting API"
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  tests)
    shift || true
    echo "[entrypoint] waiting for database"
    python - <<'PY'
from app.lifecycle import wait_for_database
wait_for_database()
print("database is ready")
PY
    echo "[entrypoint] running test suite (conftest creates + migrates the test DB)"
    exec pytest -v "$@"
    ;;
  demo)
    shift || true
    BASE_URL="${BASE_URL:-http://web:8000}"
    echo "[entrypoint] waiting for API at ${BASE_URL}"
    until curl -sf "${BASE_URL}/health" >/dev/null; do sleep 1; done
    exec python scripts/demo.py --base-url "${BASE_URL}"
    ;;
  *)
    exec "$@"
    ;;
esac
