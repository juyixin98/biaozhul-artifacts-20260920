"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoadSqlRequest(BaseModel):
    sql: str = Field(..., description="SQLite DDL/DML fixture script")
    label: str = Field(default="inline", description="human label for the version")


class LoadDirectoryRequest(BaseModel):
    directory: str | None = Field(
        default=None, description="directory to scan; defaults to configured dir"
    )
    glob: str | None = Field(default=None, description="file glob; default *.sql")
    label: str = Field(default="fixture-directory")
    reload: bool = Field(
        default=False,
        description="if false and a version already exists, return the latest",
    )


class QueryRequest(BaseModel):
    query: dict[str, Any] = Field(..., description="relational algebra query")
    version_id: int | None = Field(
        default=None, description="input version; defaults to latest"
    )


class ExpressionTerm(BaseModel):
    witnesses: list[str]
    coefficient: int


class Expression(BaseModel):
    terms: list[ExpressionTerm]


class ResultRow(BaseModel):
    values: list[Any]
    provenance: str
    expression: Expression


class VerifyRow(ResultRow):
    # Optional independently computed expectation; when present, ``matches``
    # reports whether expression evaluation equals it.
    expected_value: float | None = None


class VerifyRequest(BaseModel):
    version_id: int | None = None
    rows: list[VerifyRow]


class ErrorResponse(BaseModel):
    error: str
    category: str
    detail: str
    request_id: str
