#!/bin/sh
set -e

# Wait for MySQL to accept connections.
until python -c "
import os, sys, MySQLdb
try:
    MySQLdb.connect(
        host=os.environ.get('MYSQL_HOST', 'db'),
        port=int(os.environ.get('MYSQL_PORT', '3306')),
        user=os.environ.get('MYSQL_USER', 'minerite'),
        passwd=os.environ.get('MYSQL_PASSWORD', 'minerite'),
        db=os.environ.get('MYSQL_DATABASE', 'minerite'),
    )
except Exception:
    sys.exit(1)
"; do
  echo "waiting for mysql..."
  sleep 2
done

python manage.py migrate --noinput

if [ "${LOAD_DEMO:-0}" = "1" ]; then
  python manage.py load_demo
fi

exec "$@"
