"""Core data types for the alignment engine.

All times are integer nanoseconds.  Two distinct time bases exist:

* ``stamp_ns`` -- *event time*, taken from the message header stamp. All
  matching decisions are made in this time base.
* ``recv_ns``  -- *arrival time*, when the aligner received the message
  (wall/ROS clock online, scenario clock offline). Used only for evidence
  and bookkeeping, never for matching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

CAMERA = "camera"
IMU = "imu"


class Status(str, Enum):
    """Decision status. Stored as TEXT in SQLite."""

    MATCHED = "MATCHED"
    CAMERA_UNMATCHED = "CAMERA_UNMATCHED"
    IMU_UNMATCHED = "IMU_UNMATCHED"


# Winner/reason codes attached to every decision as pairing evidence.
REASON_NEAREST = "nearest_within_tolerance"
REASON_TIE_EARLIER = "tie_equidistant_earlier_stamp"
REASON_TIE_IDENTICAL = "tie_identical_stamp_lowest_seq"
REASON_EXPIRED = "expired_no_imu_within_tolerance"
REASON_EPOCH_CLOSED = "epoch_closed_without_match"
REASON_CACHE_EVICTED = "cache_bound_forced_eviction"
REASON_IMU_EXPIRED = "imu_expired_without_camera"
REASON_IMU_CACHE = "imu_cache_bound_drop"

NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_MILLISECOND = 1_000_000


@dataclass(frozen=True)
class Event:
    """One camera frame or one IMU sample."""

    kind: str                     # CAMERA or IMU
    stamp_ns: int                 # event time (header stamp)
    recv_ns: int                  # arrival time at the aligner
    seq: int                      # per-stream producer sequence number
    payload_hash: str             # sha256 hex digest of the raw payload
    uid: int                      # engine-unique id assigned on insertion
    frame_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """The pairing evidence record for one settled camera frame (and, under
    the exclusive IMU policy, for IMU samples that expired unconsumed)."""

    epoch_id: int
    status: Status
    config_version: int
    camera_seq: int | None = None
    camera_stamp_ns: int | None = None
    imu_seq: int | None = None
    imu_stamp_ns: int | None = None
    dt_ns: int | None = None
    abs_dt_ns: int | None = None
    tie: bool = False
    within_tolerance: bool | None = None
    forced_eviction: bool = False
    reason: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    camera_payload_hash: str | None = None
    imu_payload_hash: str | None = None
    event_recv_ns: int | None = None
    settled_recv_ns: int = 0

    def to_jsonable(self) -> dict[str, Any]:
        d = {
            "epoch_id": self.epoch_id,
            "status": self.status.value if isinstance(self.status, Status) else self.status,
            "config_version": self.config_version,
            "camera_seq": self.camera_seq,
            "camera_stamp_ns": self.camera_stamp_ns,
            "imu_seq": self.imu_seq,
            "imu_stamp_ns": self.imu_stamp_ns,
            "dt_ns": self.dt_ns,
            "abs_dt_ns": self.abs_dt_ns,
            "dt_ms": (None if self.dt_ns is None else self.dt_ns / NANOSECONDS_PER_MILLISECOND),
            "tie": self.tie,
            "within_tolerance": self.within_tolerance,
            "forced_eviction": self.forced_eviction,
            "reason": self.reason,
            "candidates": self.candidates,
            "camera_payload_hash": self.camera_payload_hash,
            "imu_payload_hash": self.imu_payload_hash,
            "event_recv_ns": self.event_recv_ns,
            "settled_recv_ns": self.settled_recv_ns,
        }
        return d
