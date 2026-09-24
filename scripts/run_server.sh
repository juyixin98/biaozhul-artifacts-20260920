#!/usr/bin/env bash
# Convenience launcher. Source ROS first (rosbag2_py lives in the ROS distro).
set -euo pipefail
if [ -z "${ROS_DISTRO:-}" ] && [ -f /opt/ros/jazzy/setup.bash ]; then
    # shellcheck disable=SC1091
    source /opt/ros/jazzy/setup.bash
fi
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPLAY_BAG_ROOTS="${REPLAY_BAG_ROOTS:-$PWD/examples}"
exec python3 -m uvicorn app.main:app \
    --host "${REPLAY_HOST:-127.0.0.1}" \
    --port "${REPLAY_PORT:-8000}" "$@"
