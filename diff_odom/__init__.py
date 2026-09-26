"""差速轮编码器里程计离线计算库。

纯后端、纯离线：仅消费合成/录制的编码器采样，不连接任何硬件。
"""

from .config import RobotParams
from .encoder import unwrap_count_delta
from .integrator import arc_step, wrap_angle
from .pipeline import run_odometry

__all__ = [
    "RobotParams",
    "unwrap_count_delta",
    "arc_step",
    "wrap_angle",
    "run_odometry",
]

__version__ = "0.1.0"
