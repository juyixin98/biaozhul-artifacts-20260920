#!/usr/bin/env bash
# Run the full test suite against a real, throwaway PostgreSQL container.
#
#   ./scripts/run-tests.sh
#
# Starts postgres:16-alpine on an ephemeral host port, waits for readiness,
# runs `go test ./...` (including -race when RACE=1), and removes the container.
set -euo pipefail

cd "$(dirname "$0")/.."

NAME="gov-dbtest-$$"
PORT="${TEST_PORT:-55432}"
IMG="postgres:16-alpine"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo ">> starting $IMG on :$PORT"
docker run -d --name "$NAME" \
  -e POSTGRES_USER=gov -e POSTGRES_PASSWORD=gov -e POSTGRES_DB=gov \
  -p "$PORT:5432" "$IMG" >/dev/null

echo ">> waiting for postgres"
for _ in $(seq 1 30); do
  if docker exec "$NAME" pg_isready -U gov -d gov >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
[ "${ready:-0}" = 1 ] || { echo "postgres did not become ready"; exit 1; }

export TEST_DATABASE_URL="postgres://gov:gov@localhost:$PORT/gov?sslmode=disable"

echo ">> go test ./..."
if [ "${RACE:-0}" = "1" ]; then
  go test -race ./... "$@"
else
  go test ./... "$@"
fi
