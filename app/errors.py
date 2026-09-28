"""Structured error taxonomy.

Every failure the service can *decide* on maps to one of three verdicts:

* ``ACCEPTED``    – computation ran and passed the numeric contract;
* ``REJECTED``    – the request/model is invalid for a concrete, static reason;
* ``UNDETERMINED`` – neither accept nor reject was possible (e.g. the error
  budget against the float reference was exceeded).

Errors carry an opaque ``request_id`` and *redacted* diagnostics: shapes,
dtypes, counts and parameter values — never caller tensor payloads.
"""

from __future__ import annotations

from typing import Any


class QInferError(Exception):
    """Base class for decidable backend errors."""

    code: str = "REJECTED_INTERNAL"
    http_status: int = 400
    verdict: str = "REJECTED"

    def __init__(self, message: str, *, request_id: str | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.request_id = request_id
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "verdict": self.verdict,
                "message": self.message,
                "request_id": self.request_id,
                "details": self.details,
            }
        }


class InvalidInputError(QInferError):
    """Caller payload failed a static contract (shape, finiteness, dtype)."""

    code = "REJECTED_INVALID_INPUT"
    http_status = 422


class ModelVersionMismatchError(QInferError):
    """Requested model version is not the one bound to this deployment."""

    code = "REJECTED_VERSION_MISMATCH"
    http_status = 409


class ModelNotCalibratedError(QInferError):
    """A layer was invoked before its calibration parameters were bound."""

    code = "REJECTED_NOT_CALIBRATED"
    http_status = 409


class CalibrationError(QInferError):
    """Calibration data/parameters are invalid (non-finite, non-positive scale)."""

    code = "REJECTED_CALIBRATION_INVALID"
    http_status = 422


class NumericIndeterminacyError(QInferError):
    """Numeric contract could not be verified — result must not be trusted."""

    code = "UNDETERMINED_NUMERIC_CONTRACT"
    http_status = 422
    verdict = "UNDETERMINED"


class CoreIntegrityError(QInferError):
    """The integer core disagreed with the independent exact oracle.

    This is an implementation defect (not quantization noise and not a bad
    request), so it is surfaced as a 5xx integrity failure rather than blamed
    on the caller.
    """

    code = "REJECTED_CORE_INTEGRITY"
    http_status = 500
    verdict = "REJECTED"
