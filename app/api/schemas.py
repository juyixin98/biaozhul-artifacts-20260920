"""API 请求/响应模型（查询验证边界）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.spec.models import LexerSpecIn


class CreateSpecResponse(BaseModel):
    spec_id: int
    run_id: str


class BuildLexerRequest(BaseModel):
    spec_id: int = Field(ge=1)


class DiagnosticsOut(BaseModel):
    overlaps: list[dict]
    unreachable: list[dict]


class BuildLexerResponse(BaseModel):
    lexer_id: int
    run_id: str
    status: str
    stats: dict
    diagnostics: DiagnosticsOut


class TokenizeRequest(BaseModel):
    text: str = Field(max_length=1_000_000)


class TokenOut(BaseModel):
    type: str
    start: int
    end: int
    text: str


class TokenizeResponse(BaseModel):
    run_id: str
    tokens: list[TokenOut]


class LexerOut(BaseModel):
    lexer_id: int
    spec_id: int
    status: str
    stats: dict | None
    diagnostics: DiagnosticsOut


class SpecCreated(BaseModel):
    spec_id: int
    run_id: str


__all__ = [
    "LexerSpecIn",
    "CreateSpecResponse",
    "BuildLexerRequest",
    "BuildLexerResponse",
    "TokenizeRequest",
    "TokenizeResponse",
    "LexerOut",
    "DiagnosticsOut",
]
