"""差速轮编码器里程计离线计算库。

纯后端、纯合成数据：不连接硬件，不依赖 ROS，不做可视化。
"""

from .config import RobotParams, DiagnosticThresholds
from .integrate import Pose2D, integrate_differential
from .pipeline import run_odometry
from .unwrap import unwrap_counter_deltas

__all__ = [
    "RobotParams",
    "DiagnosticThresholds",
    "Pose2D",
    "integrate_differential",
    "run_odometry",
    "unwrap_counter_deltas",
]

__version__ = "0.1.0"
