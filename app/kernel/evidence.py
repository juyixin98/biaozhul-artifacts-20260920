"""Evidence records: every entailment the kernel reports carries a trail."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DerivationStep:
    """One hop in a derivation chain."""

    frm: str
    to: str
    kind: str          # "subclass" | "equivalence" | "intersection_intro"
    source: str        # axiom source that licenses the hop
    detail: str = ""


@dataclass(frozen=True)
class ConflictPath:
    """A witnessed contradiction: instance (or class) forced into two
    disjoint classes, with both sides' derivation chains and the disjoint
    axiom that clashes.
    """

    subject: str
    left_chain: tuple[DerivationStep, ...]
    right_chain: tuple[DerivationStep, ...]
    disjoint_classes: tuple[str, str]
    disjoint_source: str
    category: str  # "CLASS_UNSATISFIABLE" | "ONTOLOGY_INCONSISTENT"


@dataclass(frozen=True)
class EquivalenceReport:
    canonical: str
    members: tuple[str, ...]
    merge_chain: tuple[MergeRecord, ...] = field(default_factory=tuple)
    declaration_sources: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class UnsatisfiableReport:
    cls: str
    members: tuple[str, ...]
    category: str  # CLASS_UNSATISFIABLE
    conflict: ConflictPath


@dataclass(frozen=True)
class InstanceReport:
    instance: str
    asserted_types: tuple[str, ...]
    entailed_types: tuple[str, ...]
    status: str  # "satisfiable" | "in_conflict"
    conflict: ConflictPath | None = None


@dataclass(frozen=True)
class SubsumptionEdge:
    frm: str
    to: str
    source: str
    detail: str = ""
