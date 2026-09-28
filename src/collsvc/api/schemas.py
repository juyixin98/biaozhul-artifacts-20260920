"""Pydantic request/response schemas."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..collation.options import (
    CASE_FIRST_DEFAULT,
    STRENGTH_TERTIARY,
    VALID_CASE_FIRST,
    VALID_STRENGTHS,
)


class CollationOptionsIn(BaseModel):
    locale: str = Field(default="en_US", examples=["en_US", "tr_TR"])
    strength: int = Field(default=STRENGTH_TERTIARY)
    numeric: bool = False
    case_first: str = CASE_FIRST_DEFAULT


class BuildRequest(BaseModel):
    corpus_name: str = Field(
        ..., description="corpus fixture file stem under the corpus dir"
    )
    options: CollationOptionsIn = Field(default_factory=CollationOptionsIn)
    replace: bool = True


class StringRow(BaseModel):
    doc_id: str
    text: str
    sort_key_hex: str


class TraceOut(BaseModel):
    request_id: str
    index_version: str | None
    steps: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    uncertainties: list[dict[str, Any]]


class QueryResponse(BaseModel):
    success: bool
    index_version: str
    rows: list[StringRow]
    page_size: int
    matched: int
    scanned: int
    degraded: bool
    next_cursor: str | None
    trace: TraceOut


class BuildResponse(BaseModel):
    success: bool
    result: dict[str, Any]
    trace: TraceOut


class QueryOptionsBody(BaseModel):
    options: "CollationOptionsIn" = Field(default_factory=lambda: CollationOptionsIn())
    limit: int = 50
    cursor: str | None = None


class SortedBody(QueryOptionsBody):
    pass


class RangeBody(QueryOptionsBody):
    low: str
    high: str


class PrefixBody(QueryOptionsBody):
    prefix: str
    match: str = "collation"


class MetaResponse(BaseModel):
    success: bool
    built: bool
    meta: dict[str, str]
    trace: TraceOut


class ErrorResponse(BaseModel):
    success: bool = False
    error_category: str
    error: str
    trace: TraceOut


def validate_option_fields(opts: CollationOptionsIn) -> None:
    if opts.strength not in VALID_STRENGTHS:
        raise ValueError(f"strength must be one of {sorted(VALID_STRENGTHS)}")
    if opts.case_first not in VALID_CASE_FIRST:
        raise ValueError(f"case_first must be one of {sorted(VALID_CASE_FIRST)}")
