"""Robust linear regression library (pure backend, NumPy only).

Public API
----------
- :func:`huber_irls`  : Huber regression via iteratively reweighted least squares.
- :func:`ols_fit`     : Ordinary least squares (comparison baseline).
- :func:`weighted_ridge_solve` : SVD-backed weighted ridge least squares.
- :func:`handle_request` : JSON-schema validation + computation, returns JSONable dicts.
- :class:`HuberResult`, :class:`OLSResult`, :class:`RankDeficientError`,
  :class:`RequestError`.
"""

from robustreg.huber import (
    HuberResult,
    OLSResult,
    huber_irls,
    huber_objective,
    huber_weight,
    ols_fit,
)
from robustreg.wls import RankDeficientError, weighted_ridge_solve
from robustreg.validation import RequestError, parse_request

__all__ = [
    "huber_irls",
    "ols_fit",
    "weighted_ridge_solve",
    "huber_objective",
    "huber_weight",
    "HuberResult",
    "OLSResult",
    "RankDeficientError",
    "RequestError",
    "parse_request",
    "handle_request",
]

__version__ = "1.0.0"


def handle_request(request):
    """Validate a request dict and run the requested estimator.

    See README "JSON 接口" for the schema. Returns a JSON-serializable dict
    with ``ok``/``error`` envelopes; never raises for invalid input.
    """
    from robustreg.api import handle_request as _impl

    return _impl(request)
