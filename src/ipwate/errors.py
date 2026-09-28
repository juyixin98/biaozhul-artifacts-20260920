"""Domain error types.

Every failure maps to a stable ``code`` so independent tests and API clients
can assert the *failure category*, not just the presence of an exception.
"""

from __future__ import annotations


class IPWError(Exception):
    """Base class for all expected, contract-level failures."""

    code: str = "ipw_error"
    http_status: int = 422

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(IPWError):
    """Malformed request / input data (shapes, types, NaN, cardinality)."""

    code = "validation_error"
    http_status = 422


class PositivityError(IPWError):
    """Estimated propensity score is numerically zero/one (or within eps).

    The contract forbids silent denominator replacement: a fitted score that
    implies an infinite weight REJECTS the run with this category.
    """

    code = "positivity_violation"
    http_status = 422


class OverlapRejectedError(IPWError):
    """Overlap diagnostics classify the run as REJECTED.

    Raised by the pipeline; the service layer converts it into a structured
    200/422 diagnostic response carrying the full evidence record.
    """

    code = "overlap_rejected"
    http_status = 422


class ModelConvergenceError(IPWError):
    """The propensity model failed to converge within declared iterations."""

    code = "model_did_not_converge"
    http_status = 422


class CrossfitError(IPWError):
    """Structural cross-fitting problem (bad fold, empty fold class, ...)."""

    code = "crossfit_error"
    http_status = 422


class ContractMismatchError(IPWError):
    """Requested settings are inconsistent (e.g. ATT + HT combination)."""

    code = "contract_mismatch"
    http_status = 422


class StorageError(IPWError):
    """Persistence failure."""

    code = "storage_error"
    http_status = 500
