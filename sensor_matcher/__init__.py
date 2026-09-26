"""多传感器时间配对（multi-sensor temporal pairing）纯后端库。

仅依赖 Python 标准库与 NumPy，不连接任何硬件，也不包含可视化代码。
"""

from .clock import ClockCorrection, estimate_offset
from .matcher import MessageMatcher
from .models import (
    REASON_BUFFER_OVERFLOW,
    REASON_END_UNMATCHED,
    REASON_EXPIRED,
    Match,
    Message,
    Reject,
)
from .offline import OfflineResult, offline_pair

__all__ = [
    "ClockCorrection",
    "MessageMatcher",
    "Match",
    "Message",
    "Reject",
    "OfflineResult",
    "offline_pair",
    "estimate_offset",
    "REASON_EXPIRED",
    "REASON_BUFFER_OVERFLOW",
    "REASON_END_UNMATCHED",
]
