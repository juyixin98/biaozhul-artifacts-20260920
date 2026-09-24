#!/usr/bin/env bash
# Brings up PostgreSQL in Docker and runs the stub + decision server locally
# (not in containers), then prints the acceptance command. Tears everything
# down on Ctrl-C.
set -euo pipefail
cd "$(dirname "$0")/.."

PG_PORT="${PG_PORT:-5432}"
STUB_PORT="${STUB_PORT:-8081}"
HTTP_PORT="${HTTP_PORT:-8080}"
CONTAINER="rollout-dev-pg"

cleanup() {
  echo ">> stopping local services"
  [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null || true
  [[ -n "${STUB_PID:-}" ]] && kill "$STUB_PID" 2>/dev/null || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo ">> starting postgres on :$PG_PORT"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -e POSTGRES_USER=rollout -e POSTGRES_PASSWORD=rollout -e POSTGRES_DB=rollout \
  -p "$PG_PORT:5432" postgres:16-alpine >/dev/null
echo ">> waiting for postgres"
for _ in $(seq 1 30); do
  pg_isready -h localhost -p "$PG_PORT" -U rollout >/dev/null 2>&1 && break
  sleep 0.5
done

echo ">> building"
go build -o /tmp/rollout-stub ./cmd/stub
go build -o /tmp/rollout-server ./cmd/server

echo ">> starting metrics stub on :$STUB_PORT"
/tmp/rollout-stub -addr ":$STUB_PORT" & STUB_PID=$!

echo ">> starting decision server on :$HTTP_PORT"
DATABASE_URL="postgres://rollout:rollout@localhost:$PG_PORT/rollout?sslmode=disable" \
STUB_URL="http://localhost:$STUB_PORT" HTTP_ADDR=":$HTTP_PORT" \
OBSERVATION_MS="${OBSERVATION_MS:-2000}" MIN_SAMPLES="${MIN_SAMPLES:-80}" \
/tmp/rollout-server & SERVER_PID=$!

echo
echo ">> services up. Acceptance in another terminal:"
echo "   go run ./cmd/acceptance -url http://localhost:$HTTP_PORT"
echo ">> Ctrl-C to stop and remove the database container."
wait
