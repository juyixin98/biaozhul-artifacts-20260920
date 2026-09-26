"""核心数据模型。全部为不可变值对象。"""

from dataclasses import dataclass
from typing import Any

# 无法匹配的原因（有限缓存 / 流式语义）
REASON_EXPIRED = "expired_no_candidate"
"""水位线已越过该消息时间戳加容差，未来不可能再有候选，且缓存中无候选。"""

REASON_BUFFER_OVERFLOW = "buffer_overflow"
"""该流有界缓存已满，此消息（或缓存中最旧消息）被淘汰以腾出空间。"""

REASON_END_UNMATCHED = "end_of_stream_unmatched"
"""所有消息均已到达（flush)，仍未形成配对。"""


@dataclass(frozen=True)
class Message:
    """一条传感器消息。

    Attributes:
        id: 流内唯一标识（字符串）。
        timestamp: 原始时间戳（秒，浮点）。时钟校正前的读数。
        payload: 任意可 JSON 序列化的附加数据，配对过程不解释其内容。
    """

    id: str
    timestamp: float
    payload: Any = None


@dataclass(frozen=True)
class Match:
    """一对成功配对的消息。时间均为时钟校正后的统一时基。"""

    id_a: str
    id_b: str
    t_a: float
    t_b: float
    raw_a: float
    raw_b: float

    @property
    def dt(self) -> float:
        """校正后时间差 t_b - t_a。"""
        return self.t_b - self.t_a

    @property
    def dt_raw(self) -> float:
        """校正前原始时间戳差。"""
        return self.raw_b - self.raw_a

    def as_dict(self) -> dict:
        return {
            "id_a": self.id_a,
            "id_b": self.id_b,
            "t_a": self.t_a,
            "t_b": self.t_b,
            "raw_a": self.raw_a,
            "raw_b": self.raw_b,
            "dt_corrected": self.dt,
            "dt_raw": self.dt_raw,
        }


@dataclass(frozen=True)
class Reject:
    """一条无法配对的消息及其原因。"""

    stream: str  # "a" 或 "b"
    id: str
    reason: str
    timestamp: float  # 校正后时间戳

    def as_dict(self) -> dict:
        return {
            "stream": self.stream,
            "id": self.id,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }
