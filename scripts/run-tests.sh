#!/usr/bin/env bash
# Run the DAMS integration tests against a disposable PostgreSQL container.
#
# Usage:
#   scripts/run-tests.sh                 # start docker postgres, run go test
#   DAMS_TEST_DATABASE_URL=... go test  # use your own database
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -n "${DAMS_TEST_DATABASE_URL:-}" ]]; then
    exec go test ./... "$@"
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required (or set DAMS_TEST_DATABASE_URL)" >&2
    exit 1
fi

NAME="dams-test-$$"
PORT="${DAMS_TEST_PORT:-$(python3 -c '
import socket
s=socket.socket(); s.bind(("127.0.0.1",0))
print(s.getsockname()[1]); s.close()')}"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo ">> starting postgres container $NAME on port $PORT"
docker run -d --name "$NAME" \
    -e POSTGRES_USER=dams \
    -e POSTGRES_PASSWORD=dams \
    -e POSTGRES_DB=dams \
    -p "127.0.0.1:${PORT}:5432" \
    postgres:16-alpine >/dev/null

echo ">> waiting for readiness"
for _ in $(seq 1 60); do
    if docker exec "$NAME" pg_isready -U dams -d dams >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

export DAMS_TEST_DATABASE_URL="postgres://dams:dams@127.0.0.1:${PORT}/dams?sslmode=disable"
echo ">> go test ./... $*"
exec go test ./... "$@"
