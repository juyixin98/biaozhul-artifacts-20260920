"""Symbolic provenance service for positive relational queries.

Public modules:
* :mod:`provenance.rule_language` -- relational-algebra rule language + parser
* :mod:`provenance.polynomial`    -- the N[X] semantic domain
* :mod:`provenance.engine`        -- pure annotated-relational operators
* :mod:`provenance.planner`       -- version pinning, schema flow, orchestration
* :mod:`provenance.evidence_store`-- SQLite evidence/answer persistence
* :mod:`provenance.weight_check`  -- independent numeric-weight cross-check
* :mod:`provenance.api`           -- FastAPI query interface
"""
from .errors import (
    InputVersionError,
    PlanError,
    ProvenanceError,
    SnapshotError,
    TypeRuleError,
    WeightError,
)
from .polynomial import Poly
from .planner import Planner, QueryResult

__all__ = [
    "Poly",
    "Planner",
    "QueryResult",
    "ProvenanceError",
    "PlanError",
    "InputVersionError",
    "TypeRuleError",
    "WeightError",
    "SnapshotError",
]
