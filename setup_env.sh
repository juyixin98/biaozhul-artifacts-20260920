#!/usr/bin/env bash
# Source before running anything:
#   source ./setup_env.sh
# Note: no `set -u` - ROS setup files reference unbound variables.
set -eo pipefail

# --- ROS 2 Jazzy (provides rclpy / std_msgs; installed via apt, not pip) ---
if [ -z "${ROS_DISTRO:-}" ]; then
    if [ -f /opt/ros/jazzy/setup.bash ]; then
        # shellcheck disable=SC1091
        source /opt/ros/jazzy/setup.bash
    else
        echo "ERROR: ROS 2 (Jazzy) not found at /opt/ros/jazzy" >&2
        return 1 2>/dev/null || exit 1
    fi
fi

# Isolated DDS domain so this gateway cannot clash with other ROS traffic.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"
# Keep the gateway importable without an install step.
export PYTHONPATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/src${PYTHONPATH:+:$PYTHONPATH}"

echo "ROS_DISTRO=$ROS_DISTRO ROS_DOMAIN_ID=$ROS_DOMAIN_ID PYTHONPATH set to src/"
