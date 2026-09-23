"""Pydantic request/response models — the service protocol contract."""

from __future__ import annotations

import math
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

TimeUnit = Literal["s", "ms", "us", "ns"]
AccelUnit = Literal["m/s^2", "g", "mg"]
GyroUnit = Literal["rad/s", "deg/s", "dps", "rad/hr", "deg/hr"]
TempUnit = Literal["c", "k", "f"]
RepeatPolicy = Literal["first", "mean", "error"]


class Units(BaseModel):
    """Physical units of the input arrays. Processing is done in SI."""

    time: TimeUnit = "s"
    acceleration: AccelUnit = "m/s^2"
    angular_velocity: GyroUnit = "rad/s"
    temperature: TempUnit = "c"


class AxisConfig(BaseModel):
    """Explicit axis labels, in array order."""

    accelerometer: list[str] = Field(default_factory=lambda: ["x", "y", "z"])
    gyroscope: list[str] = Field(default_factory=lambda: ["x", "y", "z"])

    @field_validator("accelerometer", "gyroscope")
    @classmethod
    def _three_unique(cls, v: list[str]) -> list[str]:
        if len(v) != 3 or len(set(v)) != 3:
            raise ValueError("exactly 3 unique axis labels are required")
        return v


class DetectorConfig(BaseModel):
    """Tunables for stationary-window detection and robust estimation.

    Variance thresholds are in SI units: (m/s^2)^2 for the accelerometer,
    (rad/s)^2 for the gyroscope.
    """

    window_sec: float = Field(1.0, gt=0.0)
    min_window_samples: int = Field(10, ge=3)
    accel_variance_thresh: float = Field(0.05, gt=0.0)
    gyro_variance_thresh: float = Field(2.0e-4, gt=0.0)
    gravity_magnitude: float = Field(9.80665, gt=0.0)
    gravity_tolerance: float = Field(0.5, gt=0.0)
    gyro_mean_thresh: float = Field(0.1, gt=0.0)
    bridge_gap_sec: float = Field(0.2, ge=0.0)
    min_candidate_duration_sec: float = Field(0.5, gt=0.0)
    gap_factor: float = Field(5.0, gt=1.0)
    max_gap_sec: Optional[float] = Field(None, gt=0.0)
    spike_z_thresh: float = Field(8.0, gt=0.0)


class BiasEstimateRequest(BaseModel):
    timestamps: list[float]
    accelerometer: list[list[float]]
    gyroscope: list[list[float]]
    temperature: Optional[list[float]] = None
    units: Units = Field(default_factory=Units)
    axes: AxisConfig = Field(default_factory=AxisConfig)
    config: DetectorConfig = Field(default_factory=DetectorConfig)
    time_repeat_policy: RepeatPolicy = "first"

    @field_validator("timestamps")
    @classmethod
    def _finite_ts(cls, v: list[float]) -> list[float]:
        if not all(math.isfinite(x) for x in v):
            raise ValueError("timestamps must all be finite")
        return v

    @field_validator("accelerometer", "gyroscope")
    @classmethod
    def _finite_vec(cls, v: list[list[float]]) -> list[list[float]]:
        for row in v:
            if len(row) != 3 or not all(math.isfinite(x) for x in row):
                raise ValueError("every row must hold 3 finite numbers")
        return v

    @field_validator("temperature")
    @classmethod
    def _finite_temp(cls, v):
        if v is not None and not all(math.isfinite(x) for x in v):
            raise ValueError("temperature must all be finite")
        return v


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AxisBias(BaseModel):
    axis: str
    bias_radps: float
    bias_degps: float
    robust_mad_radps: float
    standard_error_radps: float
    ci95_radps: list[float]
    confidence: Literal["high", "medium", "low", "none"]


class GyroBiasResult(BaseModel):
    estimate_radps: list[float]
    estimate_degps: list[float]
    axes: list[AxisBias]
    method: str
    intervals_used: int
    stationary_samples: int
    stationary_duration_sec: float
    weighted_chi_square: list[float]
    chi_square_dof: int
    interval_consistency: Literal["consistent", "inconsistent", "single_interval"]


class CandidateInterval(BaseModel):
    index: int
    segment_index: int
    start_index: int
    end_index: int
    start_time_sec: float
    end_time_sec: float
    duration_sec: float
    sample_count: int
    mid_time_sec: float
    mean_temperature_c: Optional[float]
    accel_mean_mps2: list[float]
    accel_norm_mps2: float
    gravity_residual_mps2: float
    gyro_median_radps: list[float]
    gyro_mad_radps: list[float]
    gyro_mean_radps: list[float]
    gyro_rmse_radps: list[float]
    gyro_maxabs_radps: list[float]


class SegmentInfo(BaseModel):
    index: int
    start_index: int
    end_index: int
    start_time_sec: float
    end_time_sec: float
    sample_count: int
    median_dt_sec: float


class GapInfo(BaseModel):
    after_index: int
    start_time_sec: float
    end_time_sec: float
    gap_sec: float


class SpikeInfo(BaseModel):
    index: int
    time_sec: float
    sensor: Literal["accelerometer", "gyroscope"]
    axis: str
    robust_z: float
    value: float


class DriftAxis(BaseModel):
    axis: str
    slope: float
    slope_se: float
    intercept: float
    residual_std: float
    r_squared: float


class DriftFit(BaseModel):
    available: bool
    independent_variable: str
    reason: Optional[str]
    intervals: int
    spread: Optional[float]
    quality: Optional[Literal["marginal", "ok"]]
    axes: list[DriftAxis]


class ResidualSummary(BaseModel):
    gyro_rmse_radps: list[float]
    gyro_maxabs_radps: list[float]
    max_gyro_norm_in_candidates_radps: float
    accel_gravity_residual_mps2: list[float]


class DetectionDiagnostics(BaseModel):
    windows_total: int
    windows_accepted: int
    stationary_sample_fraction: float
    rejected: dict[str, int]


class ObservabilityNote(BaseModel):
    observable: bool
    detail: str


class EstimateResponse(BaseModel):
    status: Literal["ok", "no_stationary_data"]
    sample_count: int
    sample_rate_hz: float
    segments: list[SegmentInfo]
    candidates: list[CandidateInterval]
    gyroscope_bias: Optional[GyroBiasResult]
    accelerometer: dict
    residuals: Optional[ResidualSummary]
    drift_temperature: DriftFit
    drift_time: DriftFit
    observability: dict[str, ObservabilityNote]
    diagnostics: DetectionDiagnostics
    anomalies: dict
    warnings: list[str]
    units_echo: dict
