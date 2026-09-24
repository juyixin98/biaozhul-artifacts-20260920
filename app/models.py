"""Pydantic models for gate reports (the stable JSON contract)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["error", "warning", "info"]
GateStatus = Literal["fail", "pass"]


class DiffModel(BaseModel):
    """A reviewable, machine-readable difference attached to a finding."""

    kind: Literal["value", "range", "file", "presence"] = Field(
        description="value: single field differs; range: semver unsatisfied; "
        "file: bytes-level difference; presence: missing/unexpected entry"
    )
    expected: Any = None
    actual: Any = None
    unified: str | None = Field(
        default=None,
        description="unified diff text between the expected and actual snippets",
    )
    note: str | None = None


class FindingModel(BaseModel):
    code: str = Field(description="stable machine-readable finding code")
    severity: Severity
    subject: str = Field(description="the lock key or manifest path implicated")
    message: str
    chain: list[str] = Field(
        default_factory=list,
        description="shortest dependency chain from the project root to the subject",
    )
    file: str | None = Field(
        default=None, description="archive-relative path supporting the finding"
    )
    diff: DiffModel | None = None


class SummaryModel(BaseModel):
    total_lock_nodes: int = 0
    visited_nodes: int = 0
    registry_nodes: int = 0
    workspace_nodes: int = 0
    link_nodes: int = 0
    vendored_tarballs_verified: int = 0
    tarballs_missing: int = 0
    weak_integrity_algorithms: int = 0
    errors: int = 0
    warnings: int = 0
    infos: int = 0


class PlatformModel(BaseModel):
    os: str
    cpu: str
    libc: str | None = None


class ReproducibilityModel(BaseModel):
    status: bool = Field(
        description="true only if the gate could cryptographically prove every "
        "reachable registry artifact from the bundle alone"
    )
    reasons: list[str] = Field(default_factory=list)


class ReportModel(BaseModel):
    status: GateStatus
    lockfile_version: int | None = None
    package_manager: Literal["npm"] = "npm"
    platform: PlatformModel
    production: bool = False
    require_vendored_tarballs: bool = False
    summary: SummaryModel
    reproducibility: ReproducibilityModel
    findings: list[FindingModel] = Field(
        default_factory=list,
        description="errors first, then warnings, then infos; each carries its "
        "shortest root-to-subject chain",
    )
