"""多传感器时间配对离线计算库.

按时间容差将两类传感器消息配对, 支持乱序到达、不同采样频率、
时钟偏移校正与有限缓存, 并对无法配对的消息给出明确原因.
"""

from sensor_pairing.clock import (
    apply_clock_offset,
    estimate_clock_offset,
    estimate_offset_from_data,
)
from sensor_pairing.matcher import TimePairingMatcher
from sensor_pairing.models import (
    MatchResult,
    Pair,
    SensorMessage,
    StreamId,
    UnmatchedMessage,
    UnmatchedReason,
)

__all__ = [
    "MatchResult",
    "Pair",
    "SensorMessage",
    "StreamId",
    "TimePairingMatcher",
    "UnmatchedMessage",
    "UnmatchedReason",
    "apply_clock_offset",
    "estimate_clock_offset",
    "estimate_offset_from_data",
]

__version__ = "0.1.0"
