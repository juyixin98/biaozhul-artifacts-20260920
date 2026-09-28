"""Input layer: validated sparse containers and synthetic fixtures."""
from .errors import (
    DuplicateEntryError,
    FactorizationError,
    InputValidationError,
    MatrixNotFiniteError,
    MatrixNotSymmetricError,
    MatrixShapeError,
    NonPositiveDefiniteError,
    OffDiagonalLowerError,
    PivotTooSmallError,
    SymbolicStructureMismatchError,
)
from .fixtures import COOTriples
from .matrix import FLOAT_DTYPE, SparseMatrix, build_sparse_matrix

__all__ = [
    "COOTriples",
    "FLOAT_DTYPE",
    "SparseMatrix",
    "build_sparse_matrix",
    "DuplicateEntryError",
    "FactorizationError",
    "InputValidationError",
    "MatrixNotFiniteError",
    "MatrixNotSymmetricError",
    "MatrixShapeError",
    "NonPositiveDefiniteError",
    "OffDiagonalLowerError",
    "PivotTooSmallError",
    "SymbolicStructureMismatchError",
]
