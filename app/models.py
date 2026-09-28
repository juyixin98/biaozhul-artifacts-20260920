"""API 与内部传输模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class EntryModel(BaseModel):
    """单条索引条目：排序键与原文同时保存、同时返回。"""

    id: str
    seq: int
    original: str = Field(description="原文，逐字节保留")
    nfc: str = Field(description="NFC 派生形式，仅供展示")
    sort_key_hex: str = Field(description="ICU 排序键的十六进制表示")


class SortedPage(BaseModel):
    entries: list[EntryModel]
    next_cursor: str | None
    index_version: str
    request_id: str


class RangeResult(BaseModel):
    entries: list[EntryModel]
    lower_sort_key_hex: str
    upper_sort_key_hex: str
    index_version: str
    request_id: str


class VersionInfo(BaseModel):
    index_version: str | None
    kernel_version: str
    rules: dict
    icu_version: str
    entry_count: int
    needs_rebuild: bool
    request_id: str


class RebuildResult(BaseModel):
    index_version: str
    entry_count: int
    canonical_equivalence_groups: dict[str, list[str]]
    request_id: str


class ErrorBody(BaseModel):
    category: str
    message: str
    detail: dict
    request_id: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody
