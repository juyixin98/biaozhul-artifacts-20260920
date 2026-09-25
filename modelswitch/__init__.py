"""modelswitch: local model-artifact atomic-switch serving infrastructure.

Pure-Python + NumPy. No external models or datasets are downloaded; all
fixtures are reproducible synthetic artifacts generated locally.
"""

from .loader import (
    ArtifactError,
    ChecksumError,
    LoadStage,
    ModelVersion,
    ValidationError,
    WarmupError,
    load_candidate,
)
from .registry import (
    Lease,
    ModelRegistry,
    NoActiveModelError,
    RollbackUnavailableError,
)

__all__ = [
    "ArtifactError",
    "ChecksumError",
    "Lease",
    "LoadStage",
    "ModelRegistry",
    "ModelVersion",
    "NoActiveModelError",
    "RollbackUnavailableError",
    "ValidationError",
    "WarmupError",
    "load_candidate",
]

__version__ = "0.1.0"
