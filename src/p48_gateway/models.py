"""Pydantic models for the HTTP edge."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}$")


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, max_length=128, pattern=TARGET_RE)
    seq: int = Field(ge=0, le=2**63 - 1)
    command: dict
    ttl_seconds: float | None = Field(default=None, gt=0.0, le=3600.0)
