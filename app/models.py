"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DocumentIn(BaseModel):
    doc_id: str
    content_b64: str


class CreateCorpusRequest(BaseModel):
    name: str = ""
    documents: list[DocumentIn] = Field(min_length=1)


class CorpusResponse(BaseModel):
    corpus_id: str
    name: str
    created_at: str
    doc_count: int
    total_bytes: int
    index_version: int
    request_id: str


class QuerySpec(BaseModel):
    query_id: str | None = None
    # Coverage is measured in DISTINCT DOCUMENTS, never occurrence counts.
    min_docs: int = 2
    max_candidates: int | None = None


class BatchQueryRequest(BaseModel):
    queries: list[QuerySpec] = Field(min_length=1)


class OccurrenceOut(BaseModel):
    doc_id: str
    offset: int


class CandidateOut(BaseModel):
    rank: int
    length: int
    substring_b64: str
    substring_hex: str
    doc_coverage: list[str]
    doc_coverage_count: int
    occurrences: list[OccurrenceOut]
    occurrences_truncated: bool


class QueryResult(BaseModel):
    query_id: str | None
    status: str  # "ok" | "no_result" | "error"
    failure_category: str | None = None
    detail: str | None = None
    min_docs: int
    length: int = 0
    candidate_count: int = 0
    candidates_truncated: bool = False
    candidates: list[CandidateOut] = []


class BatchQueryResponse(BaseModel):
    corpus_id: str
    request_id: str
    app_version: str
    index_version: int
    results: list[QueryResult]


class ErrorBody(BaseModel):
    category: str
    detail: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    app_version: str
    index_version: int
