#!/usr/bin/env bash
# Run every automated check for the sparse-CG backend.
# Usage: ./run_tests.sh
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

echo "=== 1. Unit + integration test suite (unittest discover) ==="
"$PYTHON" -m unittest discover -s tests -v

echo
echo "=== 2. Acceptance run (known solutions, residual comparison) ==="
"$PYTHON" acceptance_run.py

echo
echo "=== 3. CLI smoke tests over examples/ ==="
fail=0
for req in examples/request_*.json; do
  name="$(basename "$req" .json)"
  set +e
  "$PYTHON" -m sparse_cg.cli -i "$req" -o "examples/responses/${name/request/response}.json"
  code=$?
  set -e
  echo "$name -> exit $code"
done

echo
echo "All checks completed."
