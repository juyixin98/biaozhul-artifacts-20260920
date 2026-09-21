"""Pydantic request/response schemas."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ArchitectureIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    spec: dict[str, Any]


class ArchitectureOut(BaseModel):
    id: str
    name: str
    version: int
    fingerprint: str
    in_features: int
    out_features: int
    spec: dict[str, Any]


class DatasetIn(BaseModel):
    feature_path: str = Field(min_length=1)
    target_path: str | None = None
    task: str


class DatasetOut(BaseModel):
    id: str
    feature_path: str
    target_path: str | None
    task: str
    summary: dict[str, Any]


class JobIn(BaseModel):
    architecture_id: str
    dataset_id: str
    hyperparams: dict[str, Any]
    epochs: int = Field(ge=1, le=10_000)
    seed: int = Field(ge=0, lt=2**31)
    val_fraction: float = Field(gt=0.0, lt=1.0)


class JobOut(BaseModel):
    id: str
    user_id: str
    architecture_id: str
    dataset_id: str
    status: str
    epochs_total: int
    epochs_done: int
    seed: int
    hyperparams: dict[str, Any]
    dataset_summary: dict[str, Any]
    error: str | None
    lease_expires_at: Any
    heartbeat_at: Any
    created_at: Any
    finished_at: Any


class EventOut(BaseModel):
    seq: int
    kind: str
    payload: dict[str, Any]
    created_at: Any
