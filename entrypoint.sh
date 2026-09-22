#!/usr/bin/env bash
set -euo pipefail

# Wait for MySQL to accept TCP connections.
python - <<'PY'
import os, socket, sys, time

host = os.environ.get("MYSQL_HOST", "db")
port = int(os.environ.get("MYSQL_PORT", "3306"))
deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            sys.exit(0)
    except OSError:
        time.sleep(1)
sys.exit(f"database {host}:{port} not reachable within 60s")
PY

python manage.py migrate --noinput
python manage.py create_default_admin

exec "$@"
