"""Domain error hierarchy for the provenance service.

Every error carries a stable ``category`` so that the API and the tests can
assert the *failure class*, not merely that "something was raised".
"""
from __future__ import annotations


class ProvenanceError(Exception):
    """Base class. ``category`` is a stable, machine-readable failure class."""

    category = "indeterminable"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class PlanError(ProvenanceError):
    """The rule/plan is malformed (unknown operator, bad column, bad arity)."""

    category = "rejected_plan"


class InputVersionError(ProvenanceError):
    """A relation or version is missing, so a same-version answer is impossible."""

    category = "rejected_input_version"


class TypeRuleError(ProvenanceError):
    """Comparison is not decidable under the supported type/NULL rules."""

    category = "indeterminate_type"


class WeightError(ProvenanceError):
    """Numeric weight annotation is missing, malformed, or references an unknown var."""

    category = "rejected_weights"


class SnapshotError(ProvenanceError):
    """Synthetic input fixture is not a valid relation."""

    category = "rejected_snapshot"
