"""Pydantic schemas (API boundary) and shared constants."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ALGO_SHA = "sha256"
SIGNATURE_ALGO = "ed25519"

JobStatus = Literal["running", "completed", "failed"]


class CalibrationInput(BaseModel):
    frame_id: str = Field(min_length=1, max_length=128)
    # 4x4 homogeneous transform expressed row-major as nested lists.
    transform: list[list[float]] = Field(min_length=4, max_length=4)
    marker_points: list[list[float]] = Field(default_factory=list)
    reprojection_error: float = Field(ge=0.0)

    @field_validator("transform")
    @classmethod
    def _check_4x4(cls, v: list[list[float]]) -> list[list[float]]:
        if any(len(row) != 4 for row in v):
            raise ValueError("transform must be a 4x4 matrix")
        return v

    @field_validator("marker_points")
    @classmethod
    def _check_points(cls, v: list[list[float]]) -> list[list[float]]:
        for p in v:
            if len(p) != 3:
                raise ValueError("each marker point must have 3 coordinates")
        return v


class BagSummary(BaseModel):
    """Human-supplied summary bound into the snapshot.

    The bag *file* digest is always computed by the server; these fields are
    descriptive metadata whose canonical JSON is covered by the snapshot id.
    """

    topic: str = Field(min_length=1, max_length=256)
    message_count: int = Field(ge=0)
    duration_sec: float = Field(ge=0.0)
    start_time: str = Field(min_length=1, max_length=64)


class CreateSnapshotRequest(BaseModel):
    bag_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    params: dict[str, Any]
    calibration_id: str = Field(min_length=1)
    algorithm_name: str = Field(min_length=1, max_length=128)
    algorithm_version: str = Field(min_length=1, max_length=64)


class CreateJobRequest(BaseModel):
    snapshot_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    seed: int = Field(ge=0)
    # --- Fault-injection hooks (used by the acceptance demo; off by default) ---
    # Recomputes the parameter digest from altered parameters halfway through the
    # run, as if params were edited while the job was executing.
    simulate_param_change: bool = False
    # Keeps the job in "running" for this many seconds (used to demonstrate
    # crash/restart recovery).
    hold_sec: float = Field(default=0.0, ge=0.0, le=30.0)


class JobView(BaseModel):
    id: str
    snapshot_id: str
    attempt_index: int
    status: JobStatus
    seed: int
    input_digest: str | None
    input_digest_end: str | None
    output_path: str | None
    output_digest: str | None
    results_summary: dict[str, Any] | None
    params_digest_start: str | None
    params_digest_end: str | None
    reproducible: bool
    reproducibility_failures: list[str]
    tamper_events: list[str]
    error: str | None
    created_at: str
    completed_at: str | None


class SnapshotView(BaseModel):
    id: str
    created_at: str
    bag_digest: str
    bag_summary: dict[str, Any]
    params: dict[str, Any]
    calibration: dict[str, Any]
    calibration_digest: str
    algorithm: dict[str, str]
    manifest: dict[str, Any]
    signature: str


class CalibrationView(BaseModel):
    id: str
    created_at: str
    digest: str
    calibration: dict[str, Any]


class IndexPublishRequest(BaseModel):
    job_ids: list[str] = Field(min_length=1)


class IndexView(BaseModel):
    id: str
    index_number: int
    published_at: str
    path: str
    digest: str
    signature: str
    entry_count: int
    entries: list[dict[str, Any]]
