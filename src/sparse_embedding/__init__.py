"""Sparse embedding gradient accumulation and optimizer service.

Layered layout:

* :mod:`sparse_embedding.tensors`  - validated tensor value objects (layer 1)
* :mod:`sparse_embedding.graph`    - aggregation / clipping primitives (layer 2)
* :mod:`sparse_embedding.optimizer`- pure optimiser row rules (layer 3a)
* :mod:`sparse_embedding.state`    - training state + sparse transaction (layer 3b)
* :mod:`sparse_embedding.persistence` - sparse transactional checkpoint (layer 4)
* :mod:`sparse_embedding.service`  - service facade (layer 5b)
* :mod:`sparse_embedding.app`      - FastAPI boundary (layer 5a)
"""

from __future__ import annotations

__version__ = "1.0.0"

from .config import ClipConfig, ClipMode, OptimizerConfig, OptimizerName, ServiceConfig, TableSpec
from .errors import (
    EmptyBatchError,
    PersistenceError,
    SparseUpdateError,
    StateShapeError,
    ValidationBatchRejectedError,
)
from .tensors import AggregatedSparseGradient, SparseGradientBatch

__all__ = [
    "__version__",
    "TableSpec",
    "OptimizerConfig",
    "OptimizerName",
    "ClipConfig",
    "ClipMode",
    "ServiceConfig",
    "SparseGradientBatch",
    "AggregatedSparseGradient",
    "SparseUpdateError",
    "ValidationBatchRejectedError",
    "EmptyBatchError",
    "PersistenceError",
    "StateShapeError",
]
