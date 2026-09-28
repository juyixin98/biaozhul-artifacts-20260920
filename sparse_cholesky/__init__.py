"""Sparse symmetric positive-definite Cholesky backend.

Layered architecture::

    sparse_cholesky.input     validated sparse containers, synthetic fixtures
    sparse_cholesky.core      etree, ordering, symbolic/numeric factorization
    sparse_cholesky.evidence  sparse residuals, reconstruction, mpmath oracle
    sparse_cholesky.api       FastAPI service + run-correlated JSONL logging
"""
from __future__ import annotations

try:  # version discovered from installed metadata when available
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("sparse-cholesky-backend")
    except PackageNotFoundError:
        __version__ = "0.0.0+local"
except Exception:  # pragma: no cover
    __version__ = "0.0.0+local"

__all__ = ["__version__"]
