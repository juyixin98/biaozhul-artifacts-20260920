#!/usr/bin/env bash
# Build and run the full acceptance suite (core C++ tests + HTTP end-to-end).
set -euo pipefail
cd "$(dirname "$0")"

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"

echo "=== C++ core tests ==="
./build/test_icp

echo
echo "=== HTTP end-to-end tests (starts its own server) ==="
python3 tests/test_http.py
