#!/usr/bin/env bash
# One-command acceptance: configure, build, run C++ tests, start the service,
# exercise it with the signed Python client, run the independent Python
# beam-by-beam reference checker, then shut down.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BUILD="${BUILD_DIR:-$ROOT/build}"
HOST="${PF_HOST:-127.0.0.1}"
PORT="${PF_PORT:-18080}"
SECRET="${PF_SECRET:-$(build/pgrid gen-secret 2>/dev/null || true)}"
if [[ -z "${SECRET}" ]]; then
  echo "cannot obtain pgrid binary / secret" >&2
  exit 1
fi

echo "== [1/5] configure =="
cmake -S . -B "$BUILD" -DCMAKE_BUILD_TYPE=Release

echo "== [2/5] build =="
cmake --build "$BUILD" -j"$(nproc)"

echo "== [3/5] C++ unit/integration tests (ctest) =="
( cd "$BUILD" && ctest --output-on-failure )

echo "== [4/5] start service on http://$HOST:$PORT =="
export PF_SECRET="$SECRET"
"$BUILD/pgrid" serve --host "$HOST" --port "$PORT" \
    --secret-from-env PF_SECRET >"$BUILD/pgrid.log" 2>&1 &
SRV_PID=$!
trap 'kill "$SRV_PID" 2>/dev/null || true' EXIT

# Wait for /health.
for _ in $(seq 1 100); do
  if curl -fsS "http://$HOST:$PORT/health" >/dev/null 2>&1; then
    echo "service ready (pid $SRV_PID)"
    break
  fi
  sleep 0.1
done
curl -fsS "http://$HOST:$PORT/health" | head -c 200; echo

echo "== [5/5] Python signed client + independent reference checker =="
python3 scripts/pf_client.py --base "http://$HOST:$PORT" demo examples/
python3 scripts/reference_check.py --base "http://$HOST:$PORT" \
    --examples examples/

echo
echo "ACCEPTANCE OK"
echo "  secret: $SECRET"
echo "  server log: $BUILD/pgrid.log"
