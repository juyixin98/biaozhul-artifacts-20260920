"""Pydantic request/response schemas."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LayerSpec(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str
    input: str
    out_features: int | None = None
    p: float | None = None


class ArchitectureCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    input_features: int = Field(ge=1, le=100_000)
    layers: list[LayerSpec] = Field(min_length=1)


class DatasetRegister(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1)
    task: str = "classification"
    label_column: str | None = None


class JobCreate(BaseModel):
    architecture_id: int
    dataset_id: int
    epochs: int = Field(ge=1, le=10_000)
    seed: int = 0
    hyperparams: dict[str, Any] | None = None


class JobAction(BaseModel):
    action: str  # pause | resume | cancel
