#!/usr/bin/env bash
# test.sh — run unit + integration tests through the ament/colcon harness.
set -euo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set +u
source /opt/ros/jazzy/setup.bash
set -u
cd "${WS}"
colcon test --packages-select action_cancel_server --event-handlers console_direct+ "$@"
colcon test-result --verbose 2>/dev/null || true
