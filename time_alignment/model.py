"""Core data model for event-time alignment.

All times are integer nanoseconds.  Event time is the *timestamp carried by the
message itself* (sensor header stamp), not the receive time.  Receive order is
the order messages arrive at the node and drives watermark progression.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional

# 100 ms out-of-order tolerance, as required.
DEFAULT_REORDER_TOLERANCE_NS = 100_000_000

# Sentinel: an IMU sample may be reused by an unlimited number of frames.
UNLIMITED_USES = -1


class RecordKind(str, Enum):
    """Types of rows appended to the append-only evidence log."""

    EPOCH = "epoch"
    PARAMS = "params"
    PAIR = "pair"
    IMU = "imu"


class PairStatus(str, Enum):
    MATCHED = "matched"
    EXPIRED_UNPAIRED = "expired_unpaired"
    STREAM_END_UNPAIRED = "stream_end_unpaired"
    EPOCH_CLOSED_UNPAIRED = "epoch_closed_unpaired"
    CAMERA_BUFFER_OVERFLOW = "camera_buffer_overflow"


class ImuStatus(str, Enum):
    UNUSED_EXPIRED = "unused_expired"          # watermark retired it, never used
    USED_EXPIRED = "used_expired"              # watermark retired it, had served
    STREAM_END = "stream_end"                  # clean stream end
    EPOCH_CLOSED = "epoch_closed"              # epoch reset / clock back-jump
    IMU_BUFFER_OVERFLOW = "imu_buffer_overflow"


class EpochReason(str, Enum):
    FIRST_EVENT = "first_event"
    CLOCK_BACKJUMP = "clock_backjump"
    RESET_EVENT = "reset_event"


@dataclass(frozen=True)
class AlignParams:
    """Alignment configuration.  One immutable snapshot per parameter version.

    Attributes:
        version: monotonically increasing configuration version.
        tolerance_ns: a frame matches an IMU sample iff
            |camera_t - imu_t| <= tolerance_ns.
        imu_exclusive: True  -> each IMU sample is consumed by at most one frame
                       False -> an IMU sample may be reused (imu_max_uses cap).
        imu_max_uses: reuse cap; UNLIMITED_USES (-1) means unlimited.
        reorder_tolerance_ns: how far (in event time) the matcher waits for late
                       messages before finalising an event as expired.
        max_camera_pending / max_imu_pending: bounded per-epoch buffers.
        max_outcomes: bound on the in-memory recent-outcomes deque.
    """

    version: int = 1
    tolerance_ns: int = 5_000_000
    imu_exclusive: bool = True
    imu_max_uses: int = 1
    reorder_tolerance_ns: int = DEFAULT_REORDER_TOLERANCE_NS
    max_camera_pending: int = 1024
    max_imu_pending: int = 4096
    max_outcomes: int = 10_000

    def validate(self) -> None:
        if self.tolerance_ns < 0:
            raise ValueError("tolerance_ns must be >= 0")
        if self.reorder_tolerance_ns < 0:
            raise ValueError("reorder_tolerance_ns must be >= 0")
        if self.max_camera_pending < 1:
            raise ValueError("max_camera_pending must be >= 1")
        if self.max_imu_pending < 1:
            raise ValueError("max_imu_pending must be >= 1")
        if not self.imu_exclusive and self.imu_max_uses not in (UNLIMITED_USES,):
            if self.imu_max_uses < 1:
                raise ValueError("imu_max_uses must be >= 1 or -1 (unlimited)")
        if self.imu_exclusive and self.imu_max_uses != 1:
            raise ValueError("imu_exclusive=True forces imu_max_uses == 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AlignParams":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass(frozen=True)
class StreamEvent:
    """One message observed on the wire.

    kind:      "camera" | "imu" | "reset"
    t_ns:      event (header) time in nanoseconds; ignored for "reset"
    recv_ns:   receive time; default equals t_ns for deterministic scenarios
    source_id: stable identity (frame_id / sequence); tie-breaks equal stamps
    epoch_hint: optional external epoch identifier, e.g. /clock resets
    seq:       receive sequence number; assigned by the matcher if omitted
    payload:   free-form per-source payload retained verbatim in evidence
    """

    kind: str
    t_ns: Optional[int]
    source_id: str = ""
    recv_ns: Optional[int] = None
    epoch_hint: Optional[str] = None
    clock_generation: int = 0
    seq: Optional[int] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ("camera", "imu", "reset"):
            raise ValueError(f"unknown event kind: {self.kind!r}")
        if self.kind != "reset" and self.t_ns is None:
            raise ValueError(f"{self.kind} event requires t_ns")


@dataclass
class Record:
    """One append-only evidence row.

    The authenticated string is
        seq || kind || epoch || params_version || canonical(payload)
    Each row stores the SHA-256 digest of that string and an HMAC-SHA256
    chained over the previous row's mac (see storage.EvidenceStore).
    """

    seq: int
    kind: RecordKind
    epoch: int
    params_version: int
    payload: dict[str, Any]
    digest: str = ""
    mac: str = ""

    def to_row(self) -> tuple[int, str, int, int, str, str, str]:
        import json

        return (
            self.seq,
            self.kind.value,
            self.epoch,
            self.params_version,
            json.dumps(self.payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False),
            self.digest,
            self.mac,
        )
