#!/usr/bin/env bash
# Start a local PostgreSQL 16 in Docker suitable for development and tests.
# Idempotent: reuses the container if it already exists.
set -euo pipefail

NAME="${INBOX_PG_CONTAINER:-inbox-pg}"
PORT="${INBOX_PG_PORT:-55432}"
USER="${INBOX_PG_USER:-inbox}"
PASS="${INBOX_PG_PASSWORD:-inbox}"
DB="${INBOX_PG_DB:-inbox}"

if ! docker inspect "$NAME" >/dev/null 2>&1; then
  docker run -d --name "$NAME" \
    -e POSTGRES_USER="$USER" \
    -e POSTGRES_PASSWORD="$PASS" \
    -e POSTGRES_DB="$DB" \
    -p "$PORT:5432" \
    postgres:16-alpine >/dev/null
  echo "created and started container $NAME on :$PORT"
else
  docker start "$NAME" >/dev/null
  echo "started existing container $NAME on :$PORT"
fi

# Wait until it accepts connections.
for _ in $(seq 1 30); do
  if docker exec "$NAME" pg_isready -U "$USER" >/dev/null 2>&1; then
    echo "postgres is ready"
    echo "DSN: postgres://$USER:$PASS@localhost:$PORT/$DB?sslmode=disable"
    exit 0
  fi
  sleep 1
done
echo "postgres did not become ready in time" >&2
exit 1
