#!/usr/bin/env bash
# End-to-end demo for the resumable DAG executor.
# Requires: a running server ($BASE, default http://localhost:8080) and curl+jq.
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"

submit() { # $1 = request file
  curl -sS -X POST "$BASE/api/dags" -H 'Content-Type: application/json' \
    --data-binary "@$1"
}
wait_terminal() { # $1 = dag id
  for _ in $(seq 1 50); do
    body=$(curl -sS "$BASE/api/dags/$1")
    st=$(printf '%s' "$body" | jq -r '.status')
    case "$st" in
      running|pending|retrying) sleep 0.2 ;;
      *) printf '%s\n' "$body"; return 0 ;;
    esac
  done
  echo "timed out waiting for $1" >&2; return 1
}

echo "== 1. Diamond DAG (10 -> [*2, +5] -> sum = 35) =="
id=$(submit examples/diamond.json | jq -r '.id')
wait_terminal "$id" | jq '{id, status, nodes: (.nodes | to_entries | map({node: .key, status: .value.status, attempts: .value.attempts, result: .value.result}))}'

echo
echo "== 2. Middle failure with 2 retries, downstream skipped =="
id=$(submit examples/middle_failure.json | jq -r '.id')
wait_terminal "$id" | jq '{id, status, nodes: (.nodes | to_entries | map({node: .key, status: .value.status, attempts: .value.attempts, error: .value.error}))}'

echo
echo "== 3. Cycle rejected (HTTP 400) =="
curl -sS -o /tmp/cycle.resp -w 'HTTP %{http_code}\n' -X POST "$BASE/api/dags" \
  -H 'Content-Type: application/json' --data-binary @examples/cycle.json
jq . /tmp/cycle.resp

echo
echo "== 4. Missing dependency rejected (HTTP 400) =="
curl -sS -o /tmp/missing.resp -w 'HTTP %{http_code}\n' -X POST "$BASE/api/dags" \
  -H 'Content-Type: application/json' --data-binary @examples/missing_dep.json
jq . /tmp/missing.resp

echo
echo "== Listing all DAGs =="
curl -sS "$BASE/api/dags" | jq -r '.[] | "\(.id)  \(.status)"'
