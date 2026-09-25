"""spectral_peak: 离线频谱峰插值服务（纯后端，无界面）。

加窗 FFT 峰检测 + 亚频点（sub-bin）插值，输出频率/幅度估计与邻峰干扰标志。
"""

from .service import analyze, analyze_request
from .detector import PeakEstimate, AnalysisConfig

__all__ = ["analyze", "analyze_request", "PeakEstimate", "AnalysisConfig"]
__version__ = "0.1.0"
