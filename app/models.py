"""Pydantic response models for the API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Relation = Literal["direct", "transitive", "unreferenced"]


class VariantInfo(BaseModel):
    qualifiers: dict[str, str]
    subpath: list[str]


class ComponentOut(BaseModel):
    node_key: str
    bom_refs: list[str]
    purl: str | None
    ecosystem: str | None
    name: str | None
    namespace: list[str]
    version: str | None
    scope: str
    relation: Relation
    merged_count: int
    variant: VariantInfo


class FindingOut(BaseModel):
    node_key: str
    component: str | None
    ecosystem: str | None
    version: str | None
    relation: Relation
    evidence_paths: list[list[str]]
    vulnerability_id: str | None
    range_id: str | None
    status: str
    severity: str | None = None
    summary: str | None = None
    range_expression: str | None = None
    detail: str


class FixtureInfo(BaseModel):
    advisory_count: int
    source: str
    live_feed: bool
    warning: str


class AnalyzeResponse(BaseModel):
    scan_id: str
    submitted_at: str
    document_ref: str | None
    spec_version: str
    input_sha256: str
    summary: dict
    components: list[ComponentOut]
    findings: list[FindingOut]
    dependency_cycles: list[list[str]]
    warnings: list[str]
    fixture: FixtureInfo


class ScanSummary(BaseModel):
    scan_id: str
    submitted_at: str
    spec_version: str
    component_count: int
    findings_count: int


class HealthResponse(BaseModel):
    status: str
    fixture_advisories: int
