#!/usr/bin/env bash
set -euo pipefail

echo "Waiting for MySQL..."
/usr/local/bin/python /app/docker/wait-for-it.py

echo "Applying database migrations..."
python manage.py migrate --noinput

echo "Collecting static files..."
python manage.py collectstatic --noinput >/dev/null 2>&1 || true

exec gunicorn revstream.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers 3 \
  --access-logfile - \
  --error-logfile -
