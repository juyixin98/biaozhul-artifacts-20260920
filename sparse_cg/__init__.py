"""Sparse Conjugate Gradient: pure NumPy CSR + preconditioned CG backend.

Public interface:
    CSRMatrix           -- compressed sparse row matrix with strict validation
    PCGConfig           -- solver configuration (tolerances, iteration bounds)
    CGResult            -- structured solver outcome / diagnostics
    solve_pcg           -- preconditioned conjugate gradient on CSR matrices
    ValidationError hierarchy -- precise error states for bad input
"""

from .csr import CSRMatrix
from .exceptions import (
    SparseCgError,
    ValidationError,
    InvalidCsrError,
    NonSymmetricMatrixError,
    NotPositiveDefiniteError,
    InvalidRequestError,
)
from .solver import PCGConfig, CGResult, solve_pcg, STATUS_MESSAGES

__all__ = [
    "CSRMatrix",
    "PCGConfig",
    "CGResult",
    "solve_pcg",
    "STATUS_MESSAGES",
    "SparseCgError",
    "ValidationError",
    "InvalidCsrError",
    "NonSymmetricMatrixError",
    "NotPositiveDefiniteError",
    "InvalidRequestError",
]

__version__ = "1.0.0"
