"""Explicit, classified error hierarchy.

Every failure mode the estimation kernel can raise is a distinct subclass so
that tests (and the HTTP layer) can assert the *failure category*, not just
that "something went wrong".
"""

from __future__ import annotations


class IPWError(Exception):
    """Base class for all contract violations in this package."""

    # Stable machine-readable code surfaced in API responses and logs.
    code: str = "ipw_error"


class DataValidationError(IPWError):
    """Rejected before estimation: malformed input at the system boundary."""

    code = "data_validation_error"


class InsufficientDataError(IPWError):
    """Not enough units (or not enough per arm/fold) to estimate anything."""

    code = "insufficient_data_error"


class PropensityScoreError(IPWError):
    """A fitted propensity score is exactly 0/1 or non-finite.

    Per contract, near-zero/one scores must NOT be silently replaced; this
    error is raised instead of substituting an epsilon denominator.
    """

    code = "propensity_score_error"


class ModelSeparationError(IPWError):
    """Treatment is perfectly predicted (separation); weights are undefined."""

    code = "model_separation_error"


class OverlapViolationError(IPWError):
    """Positivity demonstrably fails: a covariate region is single-arm only."""

    code = "overlap_violation_error"

    def __init__(self, message: str, diagnostic: object | None = None) -> None:
        super().__init__(message)
        # Attached evidence packet (DiagnosticResult) when overlap gating fails.
        self.diagnostic = diagnostic


class EstimationError(IPWError):
    """Undefined estimate, e.g. an arm carries zero total weight."""

    code = "estimation_error"
