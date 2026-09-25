#!/usr/bin/env bash
# Request samples for the training checkpoint recovery service.
#
# Start the server first:
#   python -m checkpoint_service --runs-dir ./runs serve --port 8080
#
# Then run:  bash examples/http_requests.sh
# Requires:  curl (jq optional, used for pretty printing when present).

set -u
BASE="${BASE:-http://127.0.0.1:8080}"
RUN="${RUN:-sample-run}"

if command -v jq >/dev/null 2>&1; then
  show() { jq .; }
else
  show() { cat; }
fi

echo "### 1. Health"
curl -s "$BASE/health" | show

echo; echo "### 2. Create a run (deterministic synthetic dataset + step-0 checkpoint)"
curl -s -X POST "$BASE/runs/$RUN" \
  -H 'Content-Type: application/json' \
  -d '{
        "n_samples": 512,
        "n_features": 8,
        "data_seed": 20260925,
        "train_seed": 42,
        "batch_size": 32,
        "lr": 0.05,
        "momentum": 0.9,
        "l2": 0.0001,
        "n_epochs": 4,
        "checkpoint_every": 8
      }' | show

echo; echo "### 3. Train for a few batches (process could be killed here)"
curl -s -X POST "$BASE/runs/$RUN/train" \
  -H 'Content-Type: application/json' \
  -d '{"stop_after": 5}' | show

echo; echo "### 4. Status after a (simulated) restart - progress persisted"
curl -s "$BASE/runs/$RUN/status" | show

echo; echo "### 5. Resume and train to completion"
curl -s -X POST "$BASE/runs/$RUN/train" | show

echo; echo "### 6. Inspect the committed checkpoint (all 4 mandatory sections present)"
curl -s "$BASE/runs/$RUN/checkpoint" | show

echo; echo "### 7. List all runs"
curl -s "$BASE/runs" | show

echo; echo "### Error samples"
echo "# 7a. Create the same run again -> 409"
curl -s -o - -w "\nHTTP %{http_code}\n" -X POST "$BASE/runs/$RUN" -d '{}'
echo "# 7b. Unknown run -> 404"
curl -s -o - -w "\nHTTP %{http_code}\n" "$BASE/runs/does-not-exist/status"
echo "# 7c. Invalid config -> 400"
curl -s -o - -w "\nHTTP %{http_code}\n" -X POST "$BASE/runs/bad" \
  -H 'Content-Type: application/json' -d '{"batch_size": -1}'
