"""Atomic model-artifact switching service (local ML infrastructure)."""

from .errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ManifestValidationError,
    ModelDisposedError,
    ModelSwitchError,
    NoActiveModelError,
    SwitchBusyError,
    WarmupValidationError,
)
from .loader import ArtifactLoader, LoadedModel, LoadReport
from .manager import ModelManager, SwitchResult
from .model import SimpleMLP

__all__ = [
    "ArtifactIntegrityError",
    "ArtifactLoader",
    "ArtifactNotFoundError",
    "LoadedModel",
    "LoadReport",
    "ManifestValidationError",
    "ModelDisposedError",
    "ModelManager",
    "ModelSwitchError",
    "NoActiveModelError",
    "SimpleMLP",
    "SwitchBusyError",
    "SwitchResult",
    "WarmupValidationError",
]
