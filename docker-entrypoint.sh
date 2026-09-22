#!/bin/sh
set -e

# Always migrate the SQLite database to the latest revision before starting.
alembic upgrade head

case "${CONTAINER_ROLE:-web}" in
  web)
    # KEX_EMBED_WORKER=1 runs one worker thread inside the web container,
    # so the single-container demo needs no separate worker service.
    exec gunicorn \
      --bind "0.0.0.0:${PORT:-8000}" \
      --workers "${GUNICORN_WORKERS:-1}" \
      --threads "${GUNICORN_THREADS:-4}" \
      --timeout 60 \
      "kex.app:create_app()"
    ;;
  worker)
    exec python -m kex.cli worker
    ;;
  *)
    echo "unknown CONTAINER_ROLE: ${CONTAINER_ROLE}" >&2
    exit 1
    ;;
esac
