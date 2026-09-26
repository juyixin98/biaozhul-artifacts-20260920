"""数据模型定义."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class StreamId(enum.Enum):
    """传感器流标识."""

    A = "a"
    B = "b"

    @property
    def opposite(self) -> "StreamId":
        return StreamId.B if self is StreamId.A else StreamId.A


class UnmatchedReason(enum.Enum):
    """消息无法配对的原因."""

    #: 有限缓存已满, 最旧的消息在等到配对前被挤出(过期).
    EVICTED_CACHE_FULL = "evicted_cache_full"
    #: 流结束时仍缓存的消息: 对方流中不存在容差内的候选.
    NO_MATCH_WITHIN_TOLERANCE = "no_match_within_tolerance"


@dataclass(frozen=True)
class SensorMessage:
    """一条传感器消息.

    Attributes:
        msg_id: 消息唯一标识(在所属流内唯一).
        timestamp: 传感器原始时间戳(秒).
        data: 任意负载(如位置、姿态), 配对逻辑不解释其内容.
    """

    msg_id: str
    timestamp: float
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.msg_id, "t": self.timestamp, "data": self.data}


@dataclass(frozen=True)
class Pair:
    """一对配对成功的消息.

    Attributes:
        msg_a: 流 A 消息.
        msg_b: 流 B 消息.
        time_diff: 校正后时间差 t_a - t_b(秒).
    """

    msg_a: SensorMessage
    msg_b: SensorMessage
    time_diff: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "a": self.msg_a.to_dict(),
            "b": self.msg_b.to_dict(),
            "time_diff": self.time_diff,
        }


@dataclass(frozen=True)
class UnmatchedMessage:
    """一条未能配对的消息及其原因."""

    stream: StreamId
    message: SensorMessage
    reason: UnmatchedReason

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream.value,
            "message": self.message.to_dict(),
            "reason": self.reason.value,
        }


@dataclass
class MatchResult:
    """配对运行的完整结果."""

    pairs: list[Pair] = field(default_factory=list)
    unmatched: list[UnmatchedMessage] = field(default_factory=list)

    def unmatched_for(self, stream: StreamId) -> list[UnmatchedMessage]:
        return [u for u in self.unmatched if u.stream is stream]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pairs": [p.to_dict() for p in self.pairs],
            "unmatched": [u.to_dict() for u in self.unmatched],
            "stats": {
                "num_pairs": len(self.pairs),
                "num_unmatched_a": len(self.unmatched_for(StreamId.A)),
                "num_unmatched_b": len(self.unmatched_for(StreamId.B)),
            },
        }
