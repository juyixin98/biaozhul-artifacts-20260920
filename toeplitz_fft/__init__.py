"""Toeplitz matrix-vector / batched matmat backend via circulant embedding + FFT."""

from .cache import PlanCache
from .config import ToeplitzConfig
from .embedding import circulant_first_column, embedding_length
from .errors import (
    InconsistentToeplitzError,
    ShapeMismatchError,
    ToeplitzError,
    UnsupportedModeError,
)
from .kernel import matmat, matvec

__all__ = [
    "PlanCache",
    "ToeplitzConfig",
    "circulant_first_column",
    "embedding_length",
    "matvec",
    "matmat",
    "ToeplitzError",
    "InconsistentToeplitzError",
    "ShapeMismatchError",
    "UnsupportedModeError",
]

__version__ = "1.0.0"
