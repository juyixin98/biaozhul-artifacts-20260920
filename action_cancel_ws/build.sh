#!/usr/bin/env bash
# build.sh — build both ROS 2 packages.
#   ./build.sh            incremental build (symlink-install)
#   ./build.sh --clean    wipe build/install/log first, then build
#   ./build.sh --help     forward extra args, e.g. --packages-select
set -euo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
set +u
source /opt/ros/jazzy/setup.bash
set -u

CLEAN=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--clean" ]; then CLEAN=1; else ARGS+=("$a"); fi
done

if [ "$CLEAN" = "1" ]; then
  echo "== removing build/ install/ log/"
  rm -rf "${WS}/build" "${WS}/install" "${WS}/log"
fi

cd "${WS}"
colcon build --symlink-install "${ARGS[@]}"
echo
echo "Build complete. In every new shell run:"
echo "  source /opt/ros/jazzy/setup.bash && source ${WS}/install/setup.bash"
