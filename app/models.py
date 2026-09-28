"""Pydantic API schemas.

Request, mutation and response models are intentionally separate types per
the FastAPI conventions; offsets are model-validated so an obviously invalid
request is rejected at the boundary.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CreateDocumentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    content: str = Field(default="", max_length=2_000_000)


class EditRequest(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    replacement: str = Field(default="", max_length=1_000_000)
    base_version: int = Field(ge=1)


class AnalyzeRequest(BaseModel):
    """Stateless analysis of ad-hoc text (nothing is persisted)."""

    content: str = Field(max_length=2_000_000)
    offset: int | None = Field(default=None, ge=0)


class DefectModel(BaseModel):
    category: str
    type: str
    interval: list[int]
    open_offset: int | None
    close_offset: int | None


class MatchResponse(BaseModel):
    matched: bool
    offset: int
    partner_offset: int | None = None
    open_offset: int | None = None
    close_offset: int | None = None
    type: str | None = None
    version: int
    defect: DefectModel | None = None


class EditResponse(BaseModel):
    document_id: int
    version: int
    length: int
    invalidated_window: list[int]
    blocks_removed: int
    blocks_added: int
    blocks_total: int
    rescanned_chars: int
    mask_blocks_absorbed: int


class DocumentModel(BaseModel):
    id: int
    name: str
    lexicon_name: str
    length: int
    version: int


class DefectsResponse(BaseModel):
    balanced: bool
    version: int
    length: int
    shortest: DefectModel | None
    defects: list[DefectModel]


class VerifyResponse(BaseModel):
    agrees: bool
    version: int
    balanced: bool
    defect_count: int
    disagreements: list[dict]


class AnalyzeResponse(BaseModel):
    balanced: bool
    length: int
    defect_count: int
    shortest: DefectModel | None
    defects: list[DefectModel]
    match_at: MatchResponse | None = None
