"""Continuous/non-contiguous strided tensor backend.

Subsystems:
* :mod:`tensor_backend.tensor`     – tensor type, layout math, storage, ops
* :mod:`tensor_backend.graph`      – eager computation graph / tracing
* :mod:`tensor_backend.training`   – versioned training state (SGD)
* :mod:`tensor_backend.validation` – NumPy-oracle numerical validation
* :mod:`tensor_backend.api`        – FastAPI service
"""
from .config import SERVICE_VERSION

__version__ = SERVICE_VERSION
