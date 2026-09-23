"""Pydantic wire protocol for the multi-target tracking API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DetectionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    x: float
    y: float
    # Opaque client-side tag (e.g. detector row id). NEVER used for
    # association; echoed only in duplicate diagnostics.
    label: str | None = None


class TrackerParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: float = 1.0
    r: float = 0.05**2
    gate_pvalue: float = Field(0.99, gt=0.0, lt=1.0)
    gate_threshold: float | None = Field(None, gt=0.0)
    hits_to_confirm: int = Field(3, ge=1)
    max_misses: int = Field(4, ge=1)
    tentative_max_misses: int = Field(2, ge=1)
    duplicate_eps: float = Field(0.1, ge=0.0)
    init_vel_var: float = Field(100.0, gt=0.0)


class CreateSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tracker: TrackerParams | None = None


class FrameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_id: int = Field(..., ge=0)
    timestamp: float
    detections: list[DetectionIn] = Field(default_factory=list)


class RunnerUp(BaseModel):
    detection_index: int
    mahalanobis_sq: float
    euclidean: float


class AssociationOut(BaseModel):
    track_id: int
    detection_index: int
    prediction: tuple[float, float]
    measurement: tuple[float, float]
    mahalanobis_sq: float
    euclidean: float
    gate_threshold: float
    in_gate: bool
    runner_up: RunnerUp | None
    selection_basis: str


class DuplicateOut(BaseModel):
    x: float
    y: float
    label: str | None
    merged_with_index: int
    centroid: tuple[float, float]


class UnmatchedDetectionOut(BaseModel):
    detection_index: int
    x: float
    y: float
    label: str | None
    new_track_id: int
    reason: str


class TrackOut(BaseModel):
    track_id: int
    status: Literal["tentative", "confirmed"]
    position: tuple[float, float]
    velocity: tuple[float, float]
    hits: int
    misses: int
    age: int
    total_hits: int
    predicted_position: tuple[float, float]
    mahalanobis_sq: float | None
    gate_threshold: float
    in_gate: bool | None


class FrameOut(BaseModel):
    frame_id: int
    timestamp: float
    dt: float
    gate_threshold: float
    associations: list[AssociationOut]
    unmatched_tracks: list[TrackOut]
    unmatched_detections: list[UnmatchedDetectionOut]
    new_tracks: list[int]
    confirmed_tracks: list[int]
    deleted_tracks: list[TrackOut]
    tracks: list[TrackOut]
    duplicate_detections: list[DuplicateOut]
    cost_matrix: list[list[float | None]]
    rows_track_ids: list[int]


class SessionOut(BaseModel):
    session_id: str
    created_at: float
    tracker: TrackerParams
    last_frame_id: int | None
    last_timestamp: float | None
    track_count: int
    confirmed_track_ids: list[int]
