"""Finite-domain CSP solving kernel: model, propagation, search."""

from .domain import DomainStore, EmptyDomain
from .models import (
    BinaryConstraint,
    BinaryRelation,
    ComparisonOp,
    CSPModel,
    RelationKind,
)
from .propagate import (
    AllDifferentUnsatisfiable,
    Propagator,
    PropagationStats,
    Reason,
    run_propagation,
)
from .search import FailureKind, SearchStatus, SearchResult, Solver

__all__ = [
    "AllDifferentUnsatisfiable",
    "BinaryConstraint",
    "BinaryRelation",
    "ComparisonOp",
    "CSPModel",
    "DomainStore",
    "EmptyDomain",
    "FailureKind",
    "Propagator",
    "PropagationStats",
    "Reason",
    "RelationKind",
    "SearchStatus",
    "SearchResult",
    "Solver",
    "run_propagation",
]
