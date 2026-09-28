"""Query validation for mining requests."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MineRequest(BaseModel):
    corpus_id: str = Field(min_length=1)
    min_support: int = Field(ge=1)
    max_pos_gap: int | None = Field(default=None, ge=1)
    max_time_gap: float | None = Field(default=None, ge=0)
    max_pattern_len: int | None = Field(default=None, ge=1, le=16)
