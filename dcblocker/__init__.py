"""音频分块去偏置（DC blocker）纯后端信号处理包。"""

from .filter import DCBlocker

__all__ = ["DCBlocker"]
__version__ = "0.1.0"
