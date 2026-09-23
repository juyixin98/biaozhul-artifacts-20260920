#!/usr/bin/env bash
# Run the whole automated test suite. No third-party dependencies required
# (Python 3 standard library only).
set -euo pipefail
cd "$(dirname "$0")"

echo "### Python version"
python3 --version

echo
echo "### Unit + acceptance + differential tests"
python3 -m unittest discover -s tests -v

echo
echo "### CLI demo: safe loop (n in 0..5, array size 5)"
python3 -m intervalai.cli analyze examples/p1_safe_loop.imp --bound n=0..5

echo
echo "### CLI demo: OOB loop (n in 0..5, array size 3)"
python3 -m intervalai.cli analyze examples/p2_oob_loop.imp --bound n=0..5

echo
echo "### CLI demo: unbounded input (no bound => [-oo,+oo])"
python3 -m intervalai.cli analyze examples/p8_unbounded_loop.imp
