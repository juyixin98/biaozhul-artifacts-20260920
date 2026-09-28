"""Symmetric real-matrix eigendecomposition service.

Public, stable surface of the package. The implementation is split into four
layers that each own real work:

* ``sym_eig.numerical``  - input validation and the numerical kernel
  (Householder tridiagonalization + implicit Wilkinson-shift QR).
* ``sym_eig.evidence``   - residual / orthogonality / reconstruction evidence,
  degenerate-subspace comparison and independent high-precision oracles.
* ``sym_eig.service``    - orchestration, error taxonomy and request tracing.
* ``sym_eig.api``        - FastAPI HTTP interface.
"""

from sym_eig.config import Settings
from sym_eig.errors import EigServiceError, ErrorCategory
from sym_eig.service.engine import EigenResponse, run_eigendecomposition
from sym_eig.version import ALGORITHM_NAME, __version__

__all__ = [
    "__version__",
    "ALGORITHM_NAME",
    "Settings",
    "EigServiceError",
    "ErrorCategory",
    "EigenResponse",
    "run_eigendecomposition",
]
