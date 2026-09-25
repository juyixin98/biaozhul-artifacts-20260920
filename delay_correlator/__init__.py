"""延迟估计相关器（纯后端、离线信号处理）。

基于归一化互相关（Normalized Cross-Correlation, NCC）的双通道延迟估计服务。
输入合成信号或本地 PCM/WAV 数据，只输出数值结果（JSON/NPZ），不含任何播放器或界面。
"""

from .config import EstimatorConfig
from .correlator import LagEstimate, estimate_lag, normalized_cross_correlation

__version__ = "1.0.0"

__all__ = [
    "EstimatorConfig",
    "LagEstimate",
    "estimate_lag",
    "normalized_cross_correlation",
    "__version__",
]
