#!/usr/bin/env bash
# verify_dependencies.sh — pin and verify the exact, real dependency set.
#
# The project deliberately has no vendored/downloaded dependencies: it uses
# the Ubuntu apt packages below, whose versions are pinned both here and in
# Dockerfile.pinned.  This script FAILS if the host does not provide the
# verified versions, so a build is reproducible.
set -euo pipefail

EXPECTED_GCC_MAJOR=13
EXPECTED_CMAKE_MIN="3.16"
EXPECTED_EIGEN="3.4.0"
EXPECTED_JSON="3.11.3"
EXPECTED_OPENSSL_MAJOR=3

ok=true
say() { echo "  $*"; }

echo "== compiler =="
if ! command -v g++ >/dev/null; then echo "MISSING g++"; ok=false
else
  v="$(g++ -dumpfullversion -dumpversion)"
  say "g++ $v (major $EXPECTED_GCC_MAJOR expected)"
  [ "${v%%.*}" = "$EXPECTED_GCC_MAJOR" ] || { echo "  unexpected g++ major"; ok=false; }
fi

echo "== cmake =="
if command -v cmake >/dev/null; then
  v="$(cmake --version | head -1 | awk '{print $3}')"
  say "cmake $v (>= $EXPECTED_CMAKE_MIN)"
  # crude version comparison
  IFS=. read -r ma mi pa <<<"$v"
  [ "$ma" -gt 3 ] || { [ "$ma" -eq 3 ] && [ "$mi" -ge 16 ]; } || { echo "  cmake too old"; ok=false; }
fi

echo "== Eigen =="
eh="$(find /usr/include /usr/local/include -path '*/Eigen/src/Core/util/Macros.h' 2>/dev/null | head -1 || true)"
if [ -z "$eh" ]; then echo "MISSING Eigen (libeigen3-dev)"; ok=false
else
  w="$(awk '/EIGEN_WORLD_VERSION/{print $3}' "$eh" | head -1)"
  m="$(awk '/EIGEN_MAJOR_VERSION/{print $3}' "$eh" | head -1)"
  p="$(awk '/EIGEN_MINOR_VERSION/{print $3}' "$eh" | head -1)"
  v="$w.$m.$p"
  say "Eigen $v at $eh (expected $EXPECTED_EIGEN)"
  [ "$v" = "$EXPECTED_EIGEN" ] || { echo "  UNEXPECTED Eigen version"; ok=false; }
fi

echo "== nlohmann/json =="
jh="$(find /usr/include /usr/local/include -path '*nlohmann/detail/abi_macros.hpp' 2>/dev/null | head -1 || true)"
if [ -z "$jh" ]; then echo "MISSING nlohmann/json (nlohmann-json3-dev)"; ok=false
else
  w="$(awk '/define NLOHMANN_JSON_VERSION_MAJOR/{print $3; exit}' "$jh")"
  m="$(awk '/define NLOHMANN_JSON_VERSION_MINOR/{print $3; exit}' "$jh")"
  p="$(awk '/define NLOHMANN_JSON_VERSION_PATCH/{print $3; exit}' "$jh")"
  v="$w.$m.$p"
  say "nlohmann/json $v (expected $EXPECTED_JSON)"
  [ "$v" = "$EXPECTED_JSON" ] || { echo "  UNEXPECTED json version"; ok=false; }
fi

echo "== OpenSSL (libcrypto) =="
pv="$(pkg-config --modversion openssl 2>/dev/null || echo unknown)"
say "openssl $pv (major $EXPECTED_OPENSSL_MAJOR expected)"
[ "${pv%%.*}" = "$EXPECTED_OPENSSL_MAJOR" ] || { echo "  OpenSSL v3 required"; ok=false; }
ls /usr/include/openssl/evp.h >/dev/null 2>&1 || { echo "MISSING libssl-dev"; ok=false; }

echo
if $ok; then
  echo "DEPENDENCY VERIFICATION PASSED (no network downloads needed at build time)"
else
  echo "DEPENDENCY VERIFICATION FAILED"
  echo "On Ubuntu 24.04 install the pinned set with:"
  echo "  sudo apt-get install -y g++-13 cmake libeigen3-dev=3.4.0-4build0.1 \\"
  echo "    nlohmann-json3-dev=3.11.3-1 libssl-dev=3.0.13-0ubuntu3.15"
  exit 1
fi
