"""Krylov subspace approximation of exp(t*A) @ v for sparse matrices.

The package never forms the dense matrix exponential of A.  It builds an
Arnoldi basis, exponentiates only the small projected Hessenberg matrix,
and advances in deterministic time segments with restarts.
"""

__version__ = "0.1.0"
