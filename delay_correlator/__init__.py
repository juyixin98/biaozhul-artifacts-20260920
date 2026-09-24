"""延迟估计相关器（纯后端，NumPy 实现）。

对外主要接口：
- :func:`delay_correlator.correlator.normalized_xcorr`：归一化互相关剖面。
- :func:`delay_correlator.estimate.estimate_window`：单窗口延迟估计与置信度判定。
- :func:`delay_correlator.service.run_request`：请求 JSON -> 结果 JSON 的离线服务入口。
"""

__version__ = "1.0.0"

from .correlator import normalized_xcorr
from .estimate import estimate_window
from .service import run_request

__all__ = ["normalized_xcorr", "estimate_window", "run_request", "__version__"]
