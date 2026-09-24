#!/usr/bin/env bash
# Configure + build (uses system Eigen by default; vendors it if absent).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! { [ -f /usr/include/eigen3/Eigen/Dense ] || pkg-config --exists eigen3 2>/dev/null; } \
   && [ ! -f third_party/eigen/Eigen/Dense ]; then
  echo "[build] system Eigen not found; vendoring pinned 3.4.0 ..."
  bash scripts/setup_deps.sh
fi

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release "$@"
cmake --build build -j"$(nproc 2>/dev/null || echo 4)"
echo "[build] artifacts: build/topp_server  build/topp_cli  build/topp_tests"
