"""Versioned, validated alignment configuration.

A config object is immutable. Parameter changes are *staged* and applied by
the engine atomically at the next processing boundary; every applied config
gets a monotonically increasing version number that is stamped onto every
decision settled under it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .types import NANOSECONDS_PER_MILLISECOND

REUSE = "reuse"
EXCLUSIVE = "exclusive"
_POLICIES = (REUSE, EXCLUSIVE)


@dataclass(frozen=True)
class AlignConfig:
    """Immutable alignment parameters. Times are nanoseconds."""

    version: int
    tolerance_ns: int = 5 * NANOSECONDS_PER_MILLISECOND
    imu_policy: str = EXCLUSIVE
    # A message whose stamp lags the stream high-water mark by at most this
    # much is accepted as late/out-of-order data inside the current epoch.
    out_of_order_ns: int = 100 * NANOSECONDS_PER_MILLISECOND
    # A backwards jump larger than this opens a new epoch.
    reset_threshold_ns: int = 100 * NANOSECONDS_PER_MILLISECOND
    camera_cache_max: int = 2048
    imu_cache_max: int = 8192

    @staticmethod
    def validate(
        tolerance_ns: int,
        imu_policy: str,
        out_of_order_ns: int,
        reset_threshold_ns: int,
        camera_cache_max: int,
        imu_cache_max: int,
    ) -> None:
        if not isinstance(tolerance_ns, int) or tolerance_ns < 0:
            raise ValueError("tolerance_ns must be a non-negative int")
        if imu_policy not in _POLICIES:
            raise ValueError(f"imu_policy must be one of {_POLICIES}, got {imu_policy!r}")
        for name, val in (
            ("out_of_order_ns", out_of_order_ns),
            ("reset_threshold_ns", reset_threshold_ns),
        ):
            if not isinstance(val, int) or val < 0:
                raise ValueError(f"{name} must be a non-negative int")
        for name, val in (
            ("camera_cache_max", camera_cache_max),
            ("imu_cache_max", imu_cache_max),
        ):
            if not isinstance(val, int) or val < 1:
                raise ValueError(f"{name} must be an int >= 1")

    def validated_replace(self, **changes: Any) -> "AlignConfig":
        """Return a copy with validated overrides (version assigned on apply)."""
        values = {
            "version": self.version,
            "tolerance_ns": self.tolerance_ns,
            "imu_policy": self.imu_policy,
            "out_of_order_ns": self.out_of_order_ns,
            "reset_threshold_ns": self.reset_threshold_ns,
            "camera_cache_max": self.camera_cache_max,
            "imu_cache_max": self.imu_cache_max,
        }
        for key, val in changes.items():
            if key not in values:
                raise ValueError(f"unknown parameter {key!r}")
            values[key] = val
        validate_kwargs = {k: v for k, v in values.items() if k != "version"}
        self.validate(**validate_kwargs)
        return replace(self, **changes)

    def to_params(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "tolerance_ns": self.tolerance_ns,
            "tolerance_ms": self.tolerance_ns / NANOSECONDS_PER_MILLISECOND,
            "imu_policy": self.imu_policy,
            "out_of_order_ns": self.out_of_order_ns,
            "out_of_order_ms": self.out_of_order_ns / NANOSECONDS_PER_MILLISECOND,
            "reset_threshold_ns": self.reset_threshold_ns,
            "reset_threshold_ms": self.reset_threshold_ns / NANOSECONDS_PER_MILLISECOND,
            "camera_cache_max": self.camera_cache_max,
            "imu_cache_max": self.imu_cache_max,
        }
