"""Pydantic request/response schemas.

Time coordinates
----------------
All host timestamps are seconds since an arbitrary host epoch (monotonic or
wall clock, caller's choice — the estimator only uses differences).

Each *sample* describes one request/response round trip plus the device
counter observed inside it:

    t0 (host send)        t1 (device counter, request  arrival on device)
    |----- uplink ------->|
                          |  device processing
    |<------ downlink ----|
    t3 (host recv)        t2 (device counter, response departure)

A device that only owns a single free-running counter reports it as
``device_counter`` and leaves ``device_counter_send`` unset; the service then
treats the observed counter as an instantaneous point bounded by the whole
RTT window (t0, t3). When the device stamps *both* request arrival and
response departure, uplink and downlink are bounded separately — which is
what allows asymmetric one-way delays to be handled instead of assumed equal.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


class Sample(BaseModel):
    """One request/response observation."""

    t0: float = Field(..., description="host time when request was sent (s)")
    t3: float = Field(..., description="host time when response was received (s)")
    device_counter: float = Field(
        ..., description="device free-running counter at observation (counter units)"
    )
    device_counter_send: Optional[float] = Field(
        default=None,
        description=(
            "device counter at response departure; if given together with "
            "device_counter (request arrival) the two bound uplink/downlink "
            "separately"
        ),
    )
    seq: Optional[int] = Field(
        default=None, description="monotonic per-device sequence number, if the device exposes one"
    )

    @field_validator("t3")
    @classmethod
    def _rtt_nonnegative(cls, v, info):
        t0 = info.data.get("t0")
        if t0 is not None and v < t0:
            raise ValueError("t3 must be >= t0 (negative round-trip time)")
        return v

    @field_validator("device_counter_send")
    @classmethod
    def _send_after_recv(cls, v, info):
        dc = info.data.get("device_counter")
        if v is not None and dc is not None and v < dc:
            raise ValueError("device_counter_send must be >= device_counter")
        return v


class IngestRequest(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=128)
    counter_modulus: Optional[float] = Field(
        default=None,
        gt=0.0,
        description=(
            "counter full-scale value M when the counter wraps mod M "
            "(e.g. 2**32); leave unset for an unbounded counter"
        ),
    )
    counter_nominal_hz: Optional[float] = Field(
        default=None,
        gt=0.0,
        description=(
            "nominal counter frequency in Hz (e.g. 1000000 for a 1 MHz tick). "
            "Needed to report offset in seconds and drift in ppm"
        ),
    )
    samples: list[Sample] = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Calibration result
# ---------------------------------------------------------------------------

#: Fitting confidence levels.
Status = Literal[
    "calibrated",        # enough evidence, line published with error bounds
    "uncertain",         # evidence insufficient / ambiguous — no trustworthy line
]

#: Discontinuity classifications.
EventType = Literal[
    "wrap",              # modular counter rollover, device clock itself continuous
    "reboot",            # device restarted: counter reset, clock state unknown
    "time_jump",         # counter leapt forward anomalously (glitch / settime)
    "drift_change",      # slope of counter vs host changed (oscillator reconfiguration…)
    "uncertain",         # discontinuity seen, cause not identifiable
]


class Interval(BaseModel):
    lower: float
    upper: float

    @field_validator("upper")
    @classmethod
    def _order(cls, v, info):
        lo = info.data.get("lower")
        if lo is not None and v < lo:
            raise ValueError("interval upper < lower")
        return v


class Discontinuity(BaseModel):
    type: EventType
    at_host_time: float = Field(..., description="host t0 of the first sample after the event")
    evidence: str = Field(..., description="human-readable explanation of the classification")
    from_segment: Optional[int] = None
    to_segment: Optional[int] = None


class SegmentReport(BaseModel):
    """Result for one continuous operating regime between discontinuities."""

    index: int
    status: Status
    n_samples_input: int
    n_samples_used: int
    n_samples_rtt_filtered: int
    host_time_range: tuple[float, float]
    counter_range_unwrapped: tuple[float, float]
    # Fitted line: host_time = intercept + slope * device_counter_unwrapped
    slope: Optional[float] = Field(
        default=None, description="seconds of host time per counter unit"
    )
    intercept: Optional[float] = Field(default=None, description="host time at unwrapped counter 0")
    drift_ppm: Optional[float] = Field(
        default=None,
        description=(
            "device tick-frequency error in ppm (point estimate); requires "
            "counter_nominal_hz. See drift_ppm_ci for its uncertainty"
        ),
    )
    drift_ppm_ci: Optional[Interval] = Field(
        default=None,
        description="95% interval for drift_ppm derived from slope_ci",
    )
    offset_seconds: Optional[float] = Field(
        default=None,
        description=(
            "host - device time offset at counter-range midpoint, seconds "
            "(device time = counter / counter_nominal_hz)"
        ),
    )
    # Error bounds — never collapsed to a point estimate.
    slope_ci: Optional[Interval] = None
    intercept_ci: Optional[Interval] = None
    offset_interval: Optional[Interval] = Field(
        default=None,
        description=(
            "asymmetric [lower, upper] offset bounds in seconds at the range "
            "midpoint; width is driven by one-way-delay asymmetry, RTT and fit"
        ),
    )
    rtt_median: Optional[float] = None
    rtt_filter_threshold: Optional[float] = None
    reason: Optional[str] = Field(
        default=None, description="why status is uncertain (when applicable)"
    )


class CalibrationResponse(BaseModel):
    device_id: str
    version: Optional[str] = None
    counter_modulus: Optional[float]
    counter_nominal_hz: Optional[float]
    status: Status
    reason: Optional[str]
    segments: list[SegmentReport]
    discontinuities: list[Discontinuity]
    published: bool = Field(
        ..., description="whether a signed, versioned model was stored for the latest segment"
    )
    valid_from_host_time: Optional[float] = None
    valid_to_host_time: Optional[float] = Field(
        default=None, description="null => model remains the active calibration until superseded"
    )
    signature: Optional[str] = Field(
        default=None, description="hex HMAC-SHA256 over the canonical published model"
    )


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


class ConvertRequest(BaseModel):
    device_id: str
    device_counter: float
    host_time_hint: Optional[float] = Field(
        default=None,
        description=(
            "approximate host time of the reading; used to pick the correct "
            "wrap count / calibration version"
        ),
    )
    version: Optional[str] = Field(
        default=None, description="pin conversion to a specific published calibration version"
    )


class ConvertResponse(BaseModel):
    device_id: str
    version: str
    status: Literal["ok", "uncertain"]
    host_time_point: Optional[float] = Field(
        default=None, description="point estimate (midpoint of the interval); null when uncertain"
    )
    host_time_interval: Optional[Interval] = Field(
        default=None, description="95%-ish bounded interval; asymmetry preserved"
    )
    in_validity_range: bool
    reason: Optional[str] = None
    signature: Optional[str] = None


# ---------------------------------------------------------------------------
# Listing / health
# ---------------------------------------------------------------------------


class ModelSummary(BaseModel):
    version: str
    device_id: str
    status: Status
    valid_from_host_time: float
    valid_to_host_time: Optional[float]
    created_at: str
    slope: Optional[float]
    offset_seconds: Optional[float]
    superseded: bool


class HealthResponse(BaseModel):
    status: str
    version: str
    devices: int
    models: int
