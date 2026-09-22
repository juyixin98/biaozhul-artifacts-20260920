#!/usr/bin/env bash
set -euo pipefail

: "${MYSQL_HOST:=db}"
: "${MYSQL_PORT:=3306}"

echo "Waiting for MySQL at ${MYSQL_HOST}:${MYSQL_PORT} ..."
until nc -z "${MYSQL_HOST}" "${MYSQL_PORT}"; do
  sleep 1
done
echo "MySQL is up."

python manage.py migrate --noinput

if [[ "${LOAD_SAMPLE_DATA:-0}" == "1" ]]; then
  python manage.py seed_demo || true
fi

case "${ROLE:-web}" in
  web)
    exec gunicorn config.wsgi:application \
      --bind "0.0.0.0:${WEB_PORT:-8000}" \
      --workers "${GUNICORN_WORKERS:-4}" \
      --timeout 120
    ;;
  scheduler)
    # Runs the 30-minute scoring loop. Idempotent: the score_run service
    # guarantees that re-runs of the same time slot produce identical output.
    exec python manage.py run_scoring_loop --interval-minutes "${SCORING_INTERVAL_MINUTES:-30}"
    ;;
  *)
    exec "$@"
    ;;
esac
