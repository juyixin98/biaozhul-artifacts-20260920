"""Domain error taxonomy.

Every failure the service can produce has a stable ``code`` so that clients
(and tests) can assert the *failure category*, not just that something failed.
"""

from __future__ import annotations


class TwoSLSError(Exception):
    """Base class for all service errors."""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class InvalidRequest(TwoSLSError):
    """Malformed input at the system boundary."""

    code = "invalid_request"
    http_status = 422


class IncompatibleShapes(InvalidRequest):
    code = "incompatible_shapes"


class InsufficientObservations(InvalidRequest):
    code = "insufficient_observations"


class NonFiniteData(InvalidRequest):
    code = "non_finite_data"


class SingularDesign(InvalidRequest):
    """Exogenous design matrix (Z incl. constant/instruments) is rank deficient."""

    code = "singular_design"


class UnidentifiedModel(TwoSLSError):
    """Order/rank condition fails: endogenous regressors not identified."""

    code = "unidentified_model"
    http_status = 422


class InapplicableTest(InvalidRequest):
    """A requested diagnostic is undefined for this specification (e.g. Sargan under exact ID)."""

    code = "inapplicable_test"
    http_status = 422
