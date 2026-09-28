"""Request/response schemas for the query API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentCreateRequest(BaseModel):
    text: str = Field(..., description="full document text")


class DocumentCreateResponse(BaseModel):
    doc_id: int
    version: int
    length: int


class DocumentStateResponse(BaseModel):
    doc_id: int
    version: int
    length: int


class IntervalModel(BaseModel):
    start: int
    end: int
    category: str


class BalanceResponse(BaseModel):
    doc_id: int
    version: int
    balanced: bool
    category: str
    interval: IntervalModel | None
    unmatched_openers: int
    unmatched_closers: int
    mismatches: int


class UnbalancedIntervalResponse(BaseModel):
    doc_id: int
    version: int
    balanced: bool
    interval: IntervalModel | None


class MatchResponse(BaseModel):
    doc_id: int
    pos: int
    category: str
    match_pos: int | None


class EditRequest(BaseModel):
    expected_version: int = Field(
        ..., ge=0, description="document version the edit offsets were computed against"
    )
    start: int = Field(..., ge=0)
    end: int = Field(..., ge=0)
    replacement: str


class EditResponse(BaseModel):
    doc_id: int
    version: int
    length: int
    rescanned_chunks: int
    total_chunks: int


class ErrorBody(BaseModel):
    category: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
