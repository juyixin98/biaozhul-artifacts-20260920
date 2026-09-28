#!/usr/bin/env bash
# Start the service on an isolated DB/port, record representative runs,
# then stop the server. Results land in docs/results/.
set -euo pipefail

cd "$(dirname "$0")/.."
. .venv/bin/activate

export PLANNER_PORT="${PLANNER_PORT:-8011}"
export PLANNER_DB="data/repro_${PLANNER_PORT}.db"
export PLANNER_LOG="logs/repro_${PLANNER_PORT}.log"
export PLANNER_BASE="http://127.0.0.1:${PLANNER_PORT}"

rm -f "$PLANNER_DB"
python -m strips_planner.service &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# Wait for health.
for _ in $(seq 1 50); do
  if curl -s "$PLANNER_BASE/health" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

python scripts/record_runs.py
echo "done. evidence db: $PLANNER_DB"
