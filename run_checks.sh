#!/usr/bin/env bash
# Run the full acceptance checks for MiniML.
set -e
cd "$(dirname "$0")"

echo "== 1. unit tests =="
python3 -m unittest discover -s tests

echo
echo "== 2. identity polymorphism =="
python3 -m miniml.cli infer examples/identity.mlml --no-trace

echo
echo "== 3. occurs check rejects recursive types (expect failure, exit 1) =="
python3 -m miniml.cli infer examples/occurs_check.mlml --no-trace && exit 1 || true

echo
echo "== 4. reference unsoundness counterexample (both modes) =="
python3 examples/demo_unsound.py --check

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
