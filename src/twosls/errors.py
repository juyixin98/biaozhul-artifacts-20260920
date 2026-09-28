"""Error taxonomy for the 2SLS service.

Error semantics (see README):

* ``ValidationError``   -> HTTP 422. Malformed payload / failed boundary checks.
* ``UnidentifiedError``-> HTTP 422 with code ``NOT_IDENTIFIED``.
                           Order/exclusion conditions fail; estimation is
                           mathematically impossible, not merely imprecise.
* ``WeakInstrumentError`` -> HTTP 422 with code ``WEAK_INSTRUMENTS`` only when
                           the caller passes ``strict=true``; otherwise weak
                           instruments are a *diagnostic on an otherwise
                           returned* estimate (status=weak / inconclusive).
* ``DegenerateDataError`` -> HTTP 422. Rank deficiency, zero residual variance,
                           NaN/Inf, or matrices that make a statistic undefined.
* ``EstimationError``   -> HTTP 500. Anything unexpected inside the kernel.

Every error carries ``request_id`` plus a machine-readable ``code`` and a
``key_state`` snapshot explaining *why* the request was rejected. The messages
contain no observation-level data.
"""
from __future__ import annotations

from typing import Any, Mapping


class TwoslsError(Exception):
    http_status = 422
    code = "TWOSLS_ERROR"

    def __init__(
        self,
        message: str,
        *,
        request_id: str,
        key_state: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.request_id = request_id
        self.key_state: dict[str, Any] = dict(key_state or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": self.request_id,
                "key_state": self.key_state,
            }
        }


class ValidationError(TwoslsError):
    code = "VALIDATION_FAILED"


class UnidentifiedError(TwoslsError):
    code = "NOT_IDENTIFIED"


class WeakInstrumentError(TwoslsError):
    code = "WEAK_INSTRUMENTS"


class DegenerateDataError(TwoslsError):
    code = "DEGENERATE_DATA"


class EstimationError(TwoslsError):
    http_status = 500
    code = "ESTIMATION_FAILED"
