"""Isolate ROS network for tests before rclpy is imported anywhere."""

import os

# Loopback-only DDS, private domain, quiet logs: tests must never touch the
# user's live ROS graph (these are forced, not just defaults).
os.environ["ROS_DOMAIN_ID"] = "87"
os.environ["ROS_LOCALHOST_ONLY"] = "1"
os.environ["RCUTILS_LOGGING_USE_STDOUT"] = "0"
os.environ["RCUTILS_LOGGING_BUFFERED_STREAM"] = "1"
os.environ["RCUTILS_LOGGING_MIN_SEVERITY"] = "FATAL"

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
