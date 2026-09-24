"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class CreateSessionRequest(BaseModel):
    bag_uri: str = Field(..., description="Path to a local ros2 bag directory")
    topics: Optional[list[str]] = Field(
        default=None, description="Allow-list of topics; null/absent means all"
    )
    rate: float = 1.0
    autoplay: bool = False
    transport: Optional[str] = Field(
        default=None, description="loopback (default) or ros"
    )


class RateRequest(BaseModel):
    rate: float = Field(..., gt=0.0, le=100.0)


class SeekRequest(BaseModel):
    seq: Optional[int] = Field(default=None, ge=1)
    timestamp_ns: Optional[int] = None
    ratio: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    play_after: Optional[bool] = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "SeekRequest":
        given = [v is not None for v in (self.seq, self.timestamp_ns, self.ratio)]
        if sum(given) != 1:
            raise ValueError("exactly one of seq/timestamp_ns/ratio is required")
        return self


class FilterRequest(BaseModel):
    # null clears the filter
    topics: Optional[list[str]] = None


class CheckpointRequest(BaseModel):
    pause: bool = True


class RestoreRequest(BaseModel):
    # Either a signed envelope (e.g. previously downloaded) or a server-side
    # checkpoint file path / id.
    envelope: Optional[dict[str, Any]] = None
    checkpoint_id: Optional[str] = None
    checkpoint_path: Optional[str] = None
    transport: Optional[str] = None
    autoplay: bool = False

    @model_validator(mode="after")
    def _one_source(self) -> "RestoreRequest":
        given = [
            self.envelope is not None,
            self.checkpoint_id is not None,
            self.checkpoint_path is not None,
        ]
        if sum(given) != 1:
            raise ValueError(
                "exactly one of envelope/checkpoint_id/checkpoint_path is required"
            )
        return self
