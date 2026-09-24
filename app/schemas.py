"""Pydantic response models for the HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EntryOut(BaseModel):
    name: str
    kind: Literal["file", "dir", "symlink", "hardlink"]
    size: int = 0
    mode: str | None = None
    target: str | None = None


class InspectResponse(BaseModel):
    sha256: str
    upload_bytes: int
    compressed: bool
    compressed_bytes: int | None = None
    decompression_ratio: float | None = None
    entry_count: int
    kind_counts: dict[str, int]
    total_declared_bytes: int = Field(
        description="Sum of regular-file sizes declared by tar headers."
    )
    safe: bool = True
    entries: list[EntryOut]


class ExtractResponse(BaseModel):
    sha256: str
    upload_bytes: int
    extract_id: str
    path: str = Field(description="Absolute published directory on the server.")
    entry_count: int
    written_bytes: int = Field(
        description="Total bytes actually written for regular-file payloads."
    )
    entries: list[EntryOut]


class ErrorResponse(BaseModel):
    error: str
    message: str
    member: str | None = None
    limit: int | None = None
    actual: int | None = None
