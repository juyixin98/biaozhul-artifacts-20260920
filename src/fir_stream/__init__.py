"""fir_stream — 流式 FIR 状态管理（纯后端离线信号处理）。

核心对象:
    StreamingFIR      单通道流式 FIR(带延迟线状态)
    StreamingFIREngine 多通道引擎,支持运行中交叉渐变切换滤波器
"""

from .core import StreamingFIR
from .engine import StreamingFIREngine

__all__ = ["StreamingFIR", "StreamingFIREngine"]
__version__ = "0.1.0"
