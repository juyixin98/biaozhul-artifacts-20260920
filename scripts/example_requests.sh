#!/usr/bin/env bash
# Worked HTTP examples. Assumes the server is running (scripts/run.sh).
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "## unique solution, entries share a 10^20 factor (exact answer 1, 2):"
curl -s -X POST "$BASE/api/v1/solve" -H 'Content-Type: application/json' -d '{
  "A":[["100000000000000000000","200000000000000000000"],
       ["300000000000000000000","500000000000000000000"]],
  "b":["500000000000000000000","1300000000000000000000"]}' | python3 -m json.tool

echo "## near-float-indistinguishable matrix: exact rank 2 vs float64 rank 1:"
curl -s -X POST "$BASE/api/v1/rank" -H 'Content-Type: application/json' -d '{
  "A":[["10000000000000001","10000000000000000"],
       ["10000000000000000","9999999999999999"]],
  "include_float_diagnosis": true}' | python3 -m json.tool
