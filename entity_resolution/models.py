"""Data and error contracts (pydantic v2).

These models are the *only* structures that cross module/service boundaries.
The SQLite layer returns plain dicts and the service maps them into these;
the HTTP layer serializes these directly. Keeping the contracts in one place
means the data contract and error contract cannot drift apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# A record id is a non-empty, whitespace-trimmed string. Constraints below use
# ``Annotated`` so validation messages are precise at the boundary.
RecordId = Annotated[str, Field(min_length=1, max_length=200)]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LinkKind(str, Enum):
    MUST = "must"
    CANNOT = "cannot"


class RecordIn(BaseModel):
    """A single input record (corpus spec)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: RecordId
    name: Annotated[str, Field(min_length=1, max_length=500)]
    language: Annotated[str, Field(default="", max_length=16)] = ""
    # Free-form structured attributes (jurisdiction, established year, ...).
    # Used as hard identity signals when present and equal, never as name text.
    attributes: dict[str, str] = Field(default_factory=dict)


class CorpusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[RecordIn]
    # Pre-existing constraints carried together with the corpus (validated
    # before any clustering runs).
    must_links: list[tuple[RecordId, RecordId]] = Field(default_factory=list)
    cannot_links: list[tuple[RecordId, RecordId]] = Field(default_factory=list)
    # Alias table: canonical surface -> list of aliases / spellings.
    aliases: dict[str, list[str]] = Field(default_factory=dict)


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: RecordId
    right: RecordId
    kind: LinkKind


class LinkResult(BaseModel):
    left: str
    right: str
    kind: LinkKind
    # True when this call actually inserted the constraint; False if an
    # equivalent constraint already existed.
    added: bool


class PairEvidence(BaseModel):
    """Per-pair candidate evidence. *Not* a clustering decision by itself."""

    model_config = ConfigDict(extra="forbid")

    left: str
    right: str
    left_canonical: str
    right_canonical: str
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    # Components that explain the score; each in [0, 1].
    token_overlap: float
    char_similarity: float
    alias_match: bool
    same_attributes: dict[str, str] = Field(default_factory=dict)
    conflicting_attributes: dict[str, str] = Field(default_factory=dict)
    # Whether the pair is above the soft candidate threshold.
    candidate: bool


class ClusterOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    members: list[str]
    locked: bool
    # Canonical display name picked from members (deterministic).
    canonical_name: str
    # Provenance: which pair edges support grouping these members.
    evidence: list[PairEvidence]


class ClusterSolution(BaseModel):
    run_id: str
    threshold: float
    clusters: list[ClusterOut]
    # Pairs that were considered as candidates but rejected, with reasons
    # ("cannot_link" / "below_threshold_after_penalty" / ...).
    rejected_pairs: list[dict]
    # The constraint graph that was enforced.
    must_links: list[tuple[str, str]]
    cannot_links: list[tuple[str, str]]
    created_at: datetime = Field(default_factory=_utcnow)


class RecordOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    language: str
    attributes: dict[str, str]
    cluster_id: str | None
    locked: bool
    version: int
    created_at: datetime


class AffectedEntities(BaseModel):
    """Result of a mutation: which entities changed and how."""

    run_id: str
    # Record ids whose cluster assignment changed.
    changed_records: list[str]
    # Cluster ids that were created / dissolved / modified.
    clusters_created: list[str]
    clusters_dissolved: list[str]
    clusters_modified: list[str]
    before: dict[str, str | None]
    after: dict[str, str | None]


class LockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: RecordId
    # Optional explicit membership; defaults to the cluster's current members.
    members: list[RecordId] | None = None
    expected_version: int | None = None


class ErrorEnvelope(BaseModel):
    """Single error shape returned by every failing HTTP endpoint."""

    model_config = ConfigDict(extra="forbid")

    error: "ErrorBody"


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    code: str
    message: str
    details: dict
    run_id: str | None = None


ErrorEnvelope.model_rebuild()
