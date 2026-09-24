"""ROS replay checkpoint backend (FastAPI + rosbag2_py).

Pure backend service that replays a *local* ros2 bag by its recorded message
timeline, with stable per-message sequence numbers, pause/resume, variable
speed, seeking (generation-based) and signed checkpoints.
"""

__version__ = "1.0.0"
