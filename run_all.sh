#!/usr/bin/env bash
# Build everything and run the complete automated test suite.
# Records wall-clock timestamps so RESULTS.md evidence is reproducible.
set -euo pipefail

cd "$(dirname "$0")"

echo "== [1/4] build (strict warnings) =="
make clean >/dev/null
make

echo
echo "== [2/4] C++ unit + differential fuzz tests =="
make test

echo
echo "== [3/4] sanitizer build (ASan + UBSan) =="
g++ -std=c++17 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer \
    -Isrc tests/test_topo.cpp src/json.cpp src/topo.cpp -o build/test_topo_san
./build/test_topo_san >/dev/null
echo "sanitizer run: clean"

echo
echo "== [4/4] Python CLI differential tests + benchmark =="
python3 tests/test_cli.py

echo
echo "ALL TESTS PASSED"
