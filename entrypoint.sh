#!/usr/bin/env bash
set -euo pipefail

: "${DB_HOST:=db}"
: "${DB_PORT:=3306}"

echo "等待 MySQL ${DB_HOST}:${DB_PORT} ..."
for i in $(seq 1 60); do
  if python - <<'PY'
import socket, os
s = socket.socket()
s.settimeout(2)
try:
    s.connect((os.environ.get("DB_HOST", "db"), int(os.environ.get("DB_PORT", "3306"))))
    s.close()
except OSError:
    raise SystemExit(1)
PY
  then
    echo "MySQL 已就绪。"
    break
  fi
  sleep 2
  if [ "$i" = "60" ]; then
    echo "等待 MySQL 超时" >&2
    exit 1
  fi
done

python manage.py migrate --noinput

if [ "${DJANGO_SEED_DEMO:-0}" = "1" ]; then
  python manage.py seed || true
fi

exec "$@"
