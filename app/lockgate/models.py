"""Pydantic models for the gate's wire protocol."""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Finding(BaseModel):
    code: str = Field(description="Stable machine-readable finding code")
    severity: Severity
    location: str = Field(description="Where in the input the problem lives")
    message: str
    chain: List[str] = Field(
        default_factory=list,
        description="Shortest dependency chain from the project root to the node",
    )
    diff: Optional[str] = Field(
        default=None,
        description="Reviewable unified diff against the expected state, when applicable",
    )
    evidence: Dict[str, Any] = Field(
        default_factory=dict,
        description="Raw values used to reach the conclusion (expected/actual/...)",
    )


class PackageSummary(BaseModel):
    root_name: Optional[str] = None
    root_version: Optional[str] = None
    lockfile_version: Optional[int] = None
    total_packages: int = 0
    registry_packages: int = 0
    workspace_links: int = 0
    optional_packages: int = 0
    duplicate_names: Dict[str, List[str]] = Field(default_factory=dict)
    cycles: List[List[str]] = Field(default_factory=list)
    platform: Dict[str, Any] = Field(default_factory=dict)
    dev_included: bool = True
    artifacts_supplied: int = 0
    artifacts_verified: int = 0


class Reproducibility(BaseModel):
    # Lockfile present is NOT proof of reproducibility.
    lockfile_present: bool = False
    pinned: bool = Field(description="Every node has resolved+integrity metadata")
    content_verified: bool = Field(
        description="Every artifact hash was recomputed from supplied tarballs"
    )
    reproducible: bool = Field(description="pinned AND content_verified")
    reasons: List[str] = Field(default_factory=list)


class AuditResponse(BaseModel):
    ok: bool = Field(description="True only when no error-severity finding exists")
    reproducibility: Reproducibility
    summary: PackageSummary
    findings: List[Finding]
    unsupported_syntax: List[str] = Field(default_factory=list)
    audited_files: List[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    version: str
